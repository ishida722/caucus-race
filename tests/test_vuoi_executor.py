import json
import sys
from pathlib import Path

import pytest
from injector import Injector

from caucus_race.adapter.executor.vuoi import VuoiConfig, VuoiExecutor, parse_outcome
from caucus_race.container import ConfiguredExecutorFactory
from caucus_race.domain.entities import Project
from caucus_race.port.executor import Brief
from tests.conftest import git

# vuoi exec の代わりに動く偽スクリプト。FAKE_VUOI_MODE で振る舞いを変える
FAKE_VUOI = """
import json, os, sys, time
from pathlib import Path

args = sys.argv[1:]
Path(os.environ["FAKE_VUOI_ARGS"]).write_text(json.dumps({"args": args, "cwd": os.getcwd()}))
mode = os.environ.get("FAKE_VUOI_MODE", "done")
brief = Path(args[args.index("--brief") + 1]).read_text()
result = Path(args[args.index("--json") + 1])
if mode == "sleep":
    time.sleep(10)
if mode == "nojson":
    print("boom", file=sys.stderr)
    sys.exit(1)
if mode == "done":
    Path("feature.txt").write_text(brief)
    out = {"status": "done", "workflow": args[1], "summary": "実装しました",
           "changed_files": ["?? feature.txt"],
           "proposals": [{"title": "ログを整理する", "body": "詳細", "kind": "debt", "evidence": []}]}
    code = 0
else:
    out = {"status": "blocked", "workflow": args[1], "summary": "途中", "blocked": "認証方式が未定"}
    code = 2
result.write_text(json.dumps(out))
sys.exit(code)
"""


@pytest.fixture
def fake_vuoi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    script = tmp_path / "fake-vuoi"
    script.write_text(f"#!{sys.executable}\n{FAKE_VUOI}")
    script.chmod(0o755)
    args_file = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_VUOI_ARGS", str(args_file))
    return script, args_file


def brief() -> Brief:
    return Brief(story_id="ST-1", title="一覧", goal="一覧を出す", decisions=["新しい順"])


def make(script: Path, **kwargs) -> VuoiExecutor:
    return VuoiExecutor(VuoiConfig(command=str(script), **kwargs))


def test_done_result_is_mapped(fake_vuoi, git_repo: Path) -> None:
    script, args_file = fake_vuoi
    result = make(script, workflow="dev", test_commands=["uv run pytest"]).run(brief(), git_repo)

    assert result.status == "done"
    assert result.summary == "実装しました"
    assert [(p.title, p.body, p.kind) for p in result.proposals] == [("ログを整理する", "詳細", "debt")]
    assert Path(result.raw["log"]).exists()

    called = json.loads(args_file.read_text())
    assert Path(called["cwd"]).resolve() == git_repo.resolve()
    args = called["args"]
    assert args[:2] == ["exec", "dev"]
    assert args[args.index("--test-command") + 1] == "uv run pytest"
    brief_path = Path(args[args.index("--brief") + 1])
    # ブリーフは作業ツリーではなく git ディレクトリに置く
    assert brief_path.is_relative_to((git_repo / ".git").resolve())
    assert "新しい順" in brief_path.read_text()
    assert git(git_repo, "status", "--porcelain") == "?? feature.txt"


def test_blocked_result_is_mapped(fake_vuoi, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_VUOI_MODE", "blocked")
    result = make(fake_vuoi[0]).run(brief(), git_repo)
    assert (result.status, result.blocked_reason) == ("blocked", "認証方式が未定")


def test_missing_json_is_error(fake_vuoi, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_VUOI_MODE", "nojson")
    result = make(fake_vuoi[0]).run(brief(), git_repo)
    assert result.status == "error"
    assert "終了コード 1" in (result.error or "")
    assert "boom" in result.raw["stderr"]


def test_timeout_is_error(fake_vuoi, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_VUOI_MODE", "sleep")
    result = make(fake_vuoi[0], timeout_seconds=1).run(brief(), git_repo)
    assert result.status == "error"
    assert "1 秒以内" in (result.error or "")


def test_missing_command_is_error(git_repo: Path) -> None:
    result = make(Path("/nonexistent/vuoi")).run(brief(), git_repo)
    assert result.status == "error"


def test_parse_outcome_rejects_unknown_status() -> None:
    assert parse_outcome({"status": "weird"}).status == "error"
    assert parse_outcome({"status": "error", "error": "x"}).error == "x"


def test_factory_builds_vuoi_executor(tmp_path: Path) -> None:
    project = Project(
        id="PRJ-1",
        name="p",
        repo_path=tmp_path,
        executor_config={"type": "vuoi", "workflow": "doc", "test_commands": ["make test"]},
    )
    executor = ConfiguredExecutorFactory().for_project(project)
    assert isinstance(executor, VuoiExecutor)
    assert executor.config.workflow == "doc"
    assert executor.config.test_commands == ["make test"]


def test_sprint_runs_with_vuoi(
    fake_vuoi, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """実際のオーケストレーターと VuoiExecutor を組み合わせて 1 スプリントを回す。"""
    from caucus_race.container import AppModule
    from caucus_race.settings import Settings
    from tests.test_sprint_flow import App

    app = App(Injector([AppModule(Settings(data_dir=tmp_path / "data"))]))
    app.projects.register("demo", git_repo, executor_config={"type": "vuoi", "command": str(fake_vuoi[0])})
    (s1,) = app.plan("一覧")
    app.planning.approve("demo")
    assert app.start_and_run().status == "review"

    story = app.story(s1.id)
    assert story.status == "done"
    assert git(git_repo, "show", "--name-only", "--format=", story.commits[0]) == "feature.txt"
    assert [s.title for s in app.backlog.backlog("demo")] == ["ログを整理する"]
