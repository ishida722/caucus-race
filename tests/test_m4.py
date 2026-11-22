"""M4: 通知・バックグラウンド実行・レビュー資料。"""

import json
import os
import sys
import time
from pathlib import Path

import pytest
from injector import Injector
from typer.testing import CliRunner

from caucus_race.adapter.executor.fake import FakeExecutor
from caucus_race.adapter.notifier import channels
from caucus_race.adapter.notifier.channels import (
    CommandNotifier,
    DesktopNotifier,
    HistoryNotifier,
    MultiNotifier,
    SlackWebhookNotifier,
)
from caucus_race.adapter.notifier.console import ConsoleNotifier, RecordingNotifier
from caucus_race.adapter.planner.fake import FakePlanner
from caucus_race.cli import main as cli
from caucus_race.container import AppModule, build_notifier
from caucus_race.domain.errors import DomainError
from caucus_race.port.notifier import HumanActionRequired
from caucus_race.port.planner import PlannedStoryDraft, ReviewGuideDraft, SprintPlanDraft
from caucus_race.runner import RunLock, spawn_detached, stop
from caucus_race.settings import NotifyConfig, Settings
from tests.conftest import FakeAdaptersModule
from tests.test_ai_planning import AIApp
from tests.test_sprint_flow import P

EVENT = HumanActionRequired(kind="review_ready", project="demo", message="レビューを待っています")


# --- 設定 ---


def test_settings_load_config_toml(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        '[notify]\ndesktop = true\ncommand = "notify-me"\nslack_webhook_env = "HOOK"\n'
    )
    settings = Settings.load(tmp_path)
    assert settings.notify == NotifyConfig(desktop=True, command=["notify-me"], slack_webhook_env="HOOK")
    assert Settings.load(tmp_path / "none").notify == NotifyConfig()


# --- 通知 ---


def test_command_notifier_passes_json_on_stdin(tmp_path: Path) -> None:
    out = tmp_path / "out.json"
    script = f"import sys; open({str(out)!r}, 'w').write(sys.stdin.read())"
    CommandNotifier([sys.executable, "-c", script]).notify(EVENT)
    payload = json.loads(out.read_text())
    assert payload["kind"] == "review_ready"
    assert payload["title"] == "caucus: demo - スプリントレビュー"


def test_command_notifier_failure_does_not_raise() -> None:
    CommandNotifier(["/nonexistent/command"]).notify(EVENT)
    CommandNotifier([sys.executable, "-c", "raise SystemExit(3)"]).notify(EVENT)


def test_slack_notifier_posts_to_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict]] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout):
        sent.append((request.full_url, json.loads(request.data)))
        return Response()

    monkeypatch.setattr(channels.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("HOOK", "https://hooks.example.com/x")
    SlackWebhookNotifier("HOOK").notify(EVENT)
    assert sent == [
        (
            "https://hooks.example.com/x",
            {"text": "*caucus: demo - スプリントレビュー*\nレビューを待っています"},
        )
    ]

    monkeypatch.delenv("HOOK")
    SlackWebhookNotifier("HOOK").notify(EVENT)  # 未設定なら警告だけ
    assert len(sent) == 1


def test_desktop_notifier_uses_osascript(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(channels.subprocess, "run", lambda argv, **_: calls.append(argv))
    DesktopNotifier().notify(EVENT)
    assert calls[0][0] == "osascript"
    assert "レビューを待っています" in calls[0][2]


def test_multi_notifier_isolates_failures(tmp_path: Path) -> None:
    class Broken:
        def notify(self, event: HumanActionRequired) -> None:
            raise RuntimeError("boom")

    recording = RecordingNotifier()
    history = HistoryNotifier(tmp_path / "n.jsonl")
    MultiNotifier([Broken(), recording, history]).notify(EVENT)
    assert recording.events == [EVENT]
    assert history.read()[0]["message"] == "レビューを待っています"


def test_build_notifier_follows_config(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path, notify=NotifyConfig(desktop=True, command=["x"], slack_webhook_env="HOOK")
    )
    notifier = build_notifier(settings)
    assert isinstance(notifier, MultiNotifier)
    assert [type(n) for n in notifier.notifiers] == [
        ConsoleNotifier,
        HistoryNotifier,
        DesktopNotifier,
        CommandNotifier,
        SlackWebhookNotifier,
    ]


# --- ロックと停止 ---


def test_run_lock(tmp_path: Path) -> None:
    lock = RunLock(tmp_path, "demo")
    assert lock.running() is None
    with lock.hold(tmp_path / "x.log"):
        info = lock.running()
        assert info is not None and info.pid == os.getpid() and info.log_path == tmp_path / "x.log"
        with pytest.raises(DomainError, match="既に実行中"), RunLock(tmp_path, "demo").hold():
            pass
        assert RunLock(tmp_path, "other").running() is None  # 別プロジェクトは並行に動ける
    assert lock.running() is None


def wait_for(condition, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.2)
    raise AssertionError("時間内に条件を満たしませんでした")


def test_stop_terminates_detached_process(tmp_path: Path) -> None:
    script = (
        "import time; from pathlib import Path; from caucus_race.runner import RunLock\n"
        f"with RunLock(Path({str(tmp_path)!r}), 'demo').hold():\n"
        "    time.sleep(60)\n"
    )
    spawn_detached([sys.executable, "-c", script], tmp_path / "log.txt")
    lock = RunLock(tmp_path, "demo")
    wait_for(lambda: lock.running() is not None)
    info = lock.running()
    assert info is not None
    stop(info)
    wait_for(lambda: lock.running() is None)


# --- CLI でのバックグラウンド実行 ---

runner = CliRunner()


def invoke(*args: str) -> str:
    result = runner.invoke(cli.app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_detached_sprint_runs_to_review(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("CAUCUS_HOME", str(home))
    cli.container.cache_clear()
    invoke("project", "add", "demo", "--repo", str(git_repo))
    invoke("story", "add", "demo", "一覧画面")
    invoke("sprint", "new", "demo", "--goal", "一覧")
    invoke("sprint", "add", "demo", "ST-1")

    output = invoke("sprint", "approve", "demo", "--start")
    assert "バックグラウンドで実行しています" in output

    wait_for(lambda: "[review]" in invoke("status"))
    assert RunLock(home / "run", "demo").running() is None
    assert "スプリント #1 は review になりました" in invoke("sprint", "logs", "demo")
    assert "[review_ready] demo" in invoke("notifications")


# --- レビュー資料 ---


@pytest.fixture
def ai_app(tmp_path: Path, git_repo: Path) -> tuple[AIApp, FakePlanner]:
    planner = FakePlanner()
    injector = Injector(
        [
            AppModule(Settings(data_dir=tmp_path / "data")),
            FakeAdaptersModule(FakeExecutor(), RecordingNotifier(), planner),
        ]
    )
    app = AIApp(injector)
    app.projects.register(P, git_repo)
    return app, planner


def test_review_report_includes_guide_and_diff(ai_app: tuple[AIApp, FakePlanner]) -> None:
    app, planner = ai_app
    story = app.backlog.add_story(P, "一覧画面", acceptance_criteria=["一覧が出る"])
    planner.plans.append(
        SprintPlanDraft(goal="一覧", stories=[PlannedStoryDraft(story_id=story.id, title="")])
    )
    app.ai.plan(P)
    app.planning.approve(P)
    app.start_and_run()

    planner.review_guides.append(
        ReviewGuideDraft(overview="一覧が見られる", steps=["uv run app", "/list を開く"], cautions=["なし"])
    )
    _, text = app.review.report(P)
    assert "## 確認手順（AI が作成）" in text
    assert "1. uv run app" in text
    assert "## 変更の概要" in text and ".caucus-fake/ST-1.md" in text
    assert "1 / 1 件完了" in text
    assert planner.contexts[-1].current_plan is not None

    # 確認手順は保存して使い回す（プランナーを再度呼ばない）
    _, again = app.review.report(P)
    assert "1. uv run app" in again


def test_review_report_without_ai_planner(tmp_path: Path, git_repo: Path) -> None:
    from tests.test_sprint_flow import App

    app = App(
        Injector(
            [
                AppModule(Settings(data_dir=tmp_path / "data")),
                FakeAdaptersModule(FakeExecutor(), RecordingNotifier()),
            ]
        )
    )
    app.projects.register(P, git_repo)
    app.plan("A")
    app.planning.approve(P)
    app.start_and_run()
    _, text = app.review.report(P)
    assert "確認手順" not in text
    assert "## 変更の概要" in text
