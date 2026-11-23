"""MCP の人間の判断にあたる操作のツール（HumanTools と server.py の登録）のテスト（ST-3）。

ツールの関数を、テスト用の DI コンテナ（偽プランナー・偽実行器・一時ディレクトリの CAUCUS_HOME）で直接呼ぶ。
ツールの説明とエラーの結果（isError）は、エージェントから見える形で確かめるため MCP のクライアントから呼ぶ。

モックするのはプロセスの起動（spawn）だけ。起動したことと、その引数（何を起動したか）を記録する。
スプリントの停止は、ロックを持つ本物のプロセスを止めて確かめる。
"""

import asyncio
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from injector import Binder, Injector, InstanceProvider, Module
from mcp import Client
from mcp.types import CallToolResult

from caucus_race.adapter.executor.fake import FakeExecutor
from caucus_race.adapter.notifier.console import RecordingNotifier
from caucus_race.adapter.planner.fake import FakePlanner
from caucus_race.container import AppModule
from caucus_race.domain.entities import Event, Feedback, Spike, Sprint, Story
from caucus_race.domain.errors import DomainError
from caucus_race.domain.status import Actor, SpikeStatus, SprintStatus, StorySource, StoryStatus
from caucus_race.mcp.human_tools import HumanTools
from caucus_race.mcp.server import create_server
from caucus_race.orchestrator import Orchestrator
from caucus_race.port.repository import Repository
from caucus_race.runner import BackgroundRunner, RunLock, refine_argv, runner_argv
from caucus_race.settings import Settings
from caucus_race.usecase.common import latest_sprint, project_by_name
from caucus_race.usecase.planning import PlanningService
from caucus_race.usecase.project import BacklogService, ProjectService
from caucus_race.usecase.sprint import SprintService
from tests.conftest import FakeAdaptersModule

P = "demo"
MANUAL = "manual-prj"
HUMAN_ONLY = "人間の明示的な指示があるときだけ呼ぶ。エージェントが自分の判断で呼ばない"
JUDGMENT_TOOLS = [
    "answer_spike",
    "approve_plan",
    "reject_plan",
    "start_sprint",
    "stop_sprint",
    "resume_sprint",
    "add_feedback",
    "complete_review",
]
REFERENCE_TOOLS = ["list_spikes", "get_review", "get_plan_status"]


# OS が割り当てることのない pid（pid_t の最大値。Linux の pid_max は最大 2^22、macOS は 99998）。
# 偽の起動はこの pid を返す。終了済みのプロセスの pid を使うと、テスト中に OS が再利用したときに、起動直後の
# 実行中の印（BackgroundRunner._launching）が生きたプロセスとみなして、テストが不安定になるため
NEVER_ASSIGNED_PID = 2**31 - 1


@dataclass
class FakeSpawn:
    """プロセスの起動の代わり。起動しようとした argv とログの保存先を記録し、pid を返す。

    返す pid は OS が割り当てることのない値（存在しないプロセス）。
    BackgroundRunner は起動した pid が生きている間を「起動直後の実行中」とみなすため、
    偽の起動では「起動したプロセスがすぐにロックを取り、終わった」扱いになる。
    """

    pid: int = NEVER_ASSIGNED_PID
    calls: list[tuple[list[str], Path]] = field(default_factory=list)

    def __call__(self, argv: list[str], log_path: Path) -> int:
        self.calls.append((argv, log_path))
        return self.pid


class Env:
    def __init__(self, tmp_path: Path, git_repo: Path) -> None:
        self.settings = Settings(data_dir=tmp_path / "data")
        self.spawn = FakeSpawn()
        self.executor = FakeExecutor()
        self.planner = FakePlanner()
        runner = BackgroundRunner(self.settings.run_dir, self.settings.log_dir, self.spawn)

        class RunnerModule(Module):
            def configure(self, binder: Binder) -> None:
                binder.bind(BackgroundRunner, to=InstanceProvider(runner))

        self.injector = Injector(
            [
                AppModule(self.settings),
                FakeAdaptersModule(self.executor, RecordingNotifier(), self.planner),
                RunnerModule(),
            ]
        )
        self.tools = self.injector.get(HumanTools)
        self.repo: Repository = self.injector.get(Repository)  # type: ignore[type-abstract]
        self.backlog = self.injector.get(BacklogService)
        self.planning = self.injector.get(PlanningService)
        projects = self.injector.get(ProjectService)
        projects.register(P, git_repo, planner_config={"type": "claude"})
        projects.register(MANUAL, git_repo)

    def planning_sprint(self, project: str = P, title: str = "一覧画面") -> Story:
        story = self.backlog.add_story(project, title)
        self.planning.new_sprint(project, "一覧が見られる")
        self.planning.add_story(project, story.id)
        return story

    def approved_sprint(self, project: str = P) -> Story:
        story = self.planning_sprint(project)
        self.planning.approve(project)
        return story

    def active_sprint(self) -> Sprint:
        self.approved_sprint()
        return self.injector.get(SprintService).start(P)

    def review_sprint(self) -> tuple[Sprint, Story]:
        sprint = self.active_sprint()
        story = self.repo.query(Story, sprint_id=sprint.id)[0]
        self.injector.get(Orchestrator).run(sprint.id)  # 偽の実行器で最後まで実行して review にする
        return self.repo.get(Sprint, sprint.id), self.repo.get(Story, story.id)

    def sprint(self, project: str = P) -> Sprint:
        sprint = latest_sprint(self.repo, project_by_name(self.repo, project))
        assert sprint is not None
        return sprint

    def events(self, kind: str) -> list[Event]:
        return [e for e in self.repo.query(Event) if e.kind == kind]


@pytest.fixture
def env(tmp_path: Path, git_repo: Path) -> Env:
    return Env(tmp_path, git_repo)


@pytest.fixture
def held_lock(env: Env) -> Iterator[None]:
    """スプリントを実行中のプロセスの代わりに、実行ロックを持つ。"""
    with RunLock(env.settings.run_dir, P).hold():
        yield


def with_client[T](env: Env, body: Callable[[Client], Awaitable[T]]) -> T:
    async def run() -> T:
        async with Client(create_server(env.injector)) as client:
            return await body(client)

    return asyncio.run(run())


def text(result: CallToolResult) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


# --- 仕様違反・異常系 ---


@pytest.mark.parametrize("comment", ["", "   "])
def test_reject_plan_without_comment_is_rejected_and_keeps_approval(env: Env, comment: str) -> None:
    """差し戻しのコメントが空のとき、エラーになり、承認済みのスプリントは承認済みのままであること。"""
    env.approved_sprint()

    with pytest.raises(DomainError, match="コメント"):
        env.tools.reject_plan(P, comment)

    sprint = env.sprint()
    assert (sprint.status, sprint.plan_comment) == (SprintStatus.APPROVED, None)


def test_start_sprint_while_running_fails_without_starting_or_spawning(env: Env, held_lock: None) -> None:
    """既に実行中（実行ロックが持たれている）のとき、start_sprint は理由を説明するエラーになり、
    スプリントを開始せず（承認済みのまま）、プロセスも起動しないこと。"""
    env.approved_sprint()

    with pytest.raises(DomainError, match="既に実行中"):
        env.tools.start_sprint(P)

    assert env.sprint().status == SprintStatus.APPROVED
    assert env.spawn.calls == []


def test_resume_sprint_while_running_fails_without_spawning(env: Env) -> None:
    """既に実行中のとき、resume_sprint は理由を説明するエラーになり、プロセスを二重に起動しないこと。"""
    env.active_sprint()

    with RunLock(env.settings.run_dir, P).hold(), pytest.raises(DomainError, match="既に実行中"):
        env.tools.resume_sprint(P)

    assert env.spawn.calls == []


def test_resume_sprint_of_not_active_sprint_fails_without_spawning(env: Env) -> None:
    """スプリントが active でない（開始前の approved）とき、resume_sprint はエラーになり、
    すぐに失敗するだけの実行プロセスを起動しないこと。"""
    env.approved_sprint()

    with pytest.raises(DomainError, match="実行中ではありません"):
        env.tools.resume_sprint(P)

    assert env.spawn.calls == []


def test_stop_sprint_when_not_running_fails(env: Env) -> None:
    """実行中のプロセスがないとき、stop_sprint は理由を説明するエラーになること。"""
    env.active_sprint()

    with pytest.raises(DomainError, match=f"{P} のスプリントは実行されていません"):
        env.tools.stop_sprint(P)


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        ("approve_plan", {"project": P}, f"{P} に進行中のスプリントがありません"),
        ("stop_sprint", {"project": P}, f"{P} のスプリントは実行されていません"),
        ("answer_spike", {"spike_id": "SP-99", "answer": "はい"}, "SP-99"),
    ],
)
def test_domain_error_becomes_tool_error_with_japanese_message(
    env: Env, name: str, arguments: dict, message: str
) -> None:
    """MCP のツールとして呼んで DomainError になったとき、トレースバックではなく isError の結果になり、
    本文に日本語の理由が入ること。"""
    result = with_client(env, lambda c: c.call_tool(name, arguments))

    assert result.is_error is True
    assert message in text(result)
    assert "Traceback" not in text(result)


# --- ツールの説明 ---


def test_judgment_tools_say_call_only_on_explicit_human_instruction(env: Env) -> None:
    """判断を伴うツールの説明には「人間の明示的な指示があるときだけ呼ぶ」制約が書かれ、
    参照だけのツールには書かれていないこと（エージェントが見る説明として）。"""
    listed = with_client(env, lambda c: c.list_tools())
    descriptions = {t.name: t.description or "" for t in listed.tools}

    missing = [n for n in JUDGMENT_TOOLS if HUMAN_ONLY not in descriptions[n]]
    wrongly = [n for n in REFERENCE_TOOLS if "人間の明示的な指示" in descriptions[n]]
    assert (missing, wrongly) == ([], [])


# --- スパイク ---


def test_list_spikes_returns_only_open_spikes_of_the_project(env: Env) -> None:
    """list_spikes は、指定したプロジェクトの未解決のスパイクだけを返すこと
    （回答済みのスパイクと、別のプロジェクトのスパイクは含めない）。"""
    story = env.planning_sprint()
    other = env.backlog.add_story(MANUAL, "別プロジェクト")
    open_spike = env.planning.add_spike(story.id, "並び順は？", options=["新しい順", "古い順"])
    answered = env.planning.add_spike(story.id, "件数は？")
    env.planning.answer_spike(answered.id, "20 件")
    env.planning.add_spike(other.id, "別の質問")

    spikes = env.tools.list_spikes(P)

    assert [(s.id, s.question, s.options) for s in spikes] == [
        (open_spike.id, "並び順は？", ["新しい順", "古い順"])
    ]


def test_answer_spike_adds_decision_to_story_as_human(env: Env) -> None:
    """answer_spike の回答は、ストーリーの決定事項に「質問 → 回答」として追加され、
    スパイクは回答済みになり、人間（Actor.HUMAN）の操作として記録されること。"""
    story = env.planning_sprint()
    spike = env.planning.add_spike(story.id, "並び順は？")

    result = env.tools.answer_spike(spike.id, "新しい順")

    assert env.repo.get(Story, story.id).decisions == ["並び順は？ → 新しい順"]
    assert env.repo.get(Spike, spike.id).status == SpikeStatus.ANSWERED
    assert [e.actor for e in env.events("spike.answered")] == [Actor.HUMAN]
    assert result.decision == "並び順は？ → 新しい順"


def test_answer_last_spike_with_ai_planner_starts_refine_in_background(env: Env) -> None:
    """AI プランナーが設定されていて、最後の未解決のスパイクに回答したとき、そのストーリーの見直しを
    別プロセスで起動し、見直しの完了を待たずに（この場では見直しを行わずに）起動したことを返すこと。"""
    story = env.planning_sprint()
    spike = env.planning.add_spike(story.id, "並び順は？")

    result = env.tools.answer_spike(spike.id, "新しい順")

    assert [argv for argv, _ in env.spawn.calls] == [refine_argv(story.id)]
    assert (result.refine_started, result.refine_pid) == (True, env.spawn.pid)
    assert result.refine_log_path == str(env.spawn.calls[0][1])
    assert env.planner.refined == []  # ツールの中では AI を呼んでいない


@pytest.mark.parametrize(
    "case",
    ["spikes-remain", "manual-planner"],
)
def test_answer_spike_does_not_start_refine_unless_conditions_hold(env: Env, case: str) -> None:
    """未解決のスパイクがまだ残っているとき、または AI プランナーが設定されていない（manual）とき、
    回答しても見直しを起動しないこと（CLI と同じ条件）。"""
    project = MANUAL if case == "manual-planner" else P
    story = env.planning_sprint(project)
    spike = env.planning.add_spike(story.id, "並び順は？")
    if case == "spikes-remain":
        env.planning.add_spike(story.id, "件数は？")

    result = env.tools.answer_spike(spike.id, "新しい順")

    assert env.spawn.calls == []
    assert result.refine_started is False


# --- 計画の承認・差し戻し ---


def test_approve_plan_approves_sprint_as_human(env: Env) -> None:
    """approve_plan で計画中のスプリントが承認済みになり、人間の操作として記録されること。"""
    env.planning_sprint()

    result = env.tools.approve_plan(P)

    assert env.sprint().status == SprintStatus.APPROVED
    assert result.status == SprintStatus.APPROVED
    assert [e.actor for e in env.events("sprint.approved")] == [Actor.HUMAN]


def test_reject_plan_returns_sprint_to_planning_with_comment_as_human(env: Env) -> None:
    """reject_plan で承認済みのスプリントが計画中に戻り、コメントが残り、人間の操作として記録されること。"""
    env.approved_sprint()

    env.tools.reject_plan(P, "ストーリーを減らしてほしい")

    sprint = env.sprint()
    assert (sprint.status, sprint.plan_comment) == (SprintStatus.PLANNING, "ストーリーを減らしてほしい")
    assert [e.actor for e in env.events("sprint.rejected")] == [Actor.HUMAN]


# --- スプリントの開始・停止・再開 ---


def test_start_sprint_launches_runner_and_returns_without_running_stories(env: Env) -> None:
    """start_sprint はスプリントを開始し（人間の操作として記録）、実行プロセスを起動して、
    ストーリーの実行を待たずにスプリント番号・ブランチ・pid・ログの保存先を返すこと。"""
    story = env.approved_sprint()

    result = env.tools.start_sprint(P)

    [(argv, log_path)] = env.spawn.calls
    assert argv == runner_argv(P, log_path)
    assert (result.sprint_number, result.branch, result.pid, result.log_path) == (
        1,
        "caucus/sprint-1",
        env.spawn.pid,
        str(log_path),
    )
    assert log_path.parent == env.settings.log_dir
    assert env.sprint().status == SprintStatus.ACTIVE
    # ストーリーはツールの中では実行されていない（別プロセスが実行する）
    assert env.repo.get(Story, story.id).status == StoryStatus.QUEUED
    assert env.executor.calls == []
    assert [e.actor for e in env.events("sprint.started")] == [Actor.HUMAN]


def test_resume_sprint_launches_runner_for_stopped_sprint(env: Env) -> None:
    """停止中（active で実行プロセスがない）のスプリントに resume_sprint を呼ぶと、実行プロセスを起動し、
    完了を待たずに pid とログの保存先を返すこと。"""
    env.active_sprint()

    result = env.tools.resume_sprint(P)

    [(argv, log_path)] = env.spawn.calls
    assert argv == runner_argv(P, log_path)
    assert (result.pid, result.log_path, result.sprint_number) == (env.spawn.pid, str(log_path), 1)
    assert env.executor.calls == []


def test_stop_sprint_terminates_running_process_and_tells_how_to_resume(env: Env) -> None:
    """実行ロックを持つ本物のプロセスに stop_sprint を呼ぶと、そのプロセスが止まり、
    返り値に pid と再開方法（resume_sprint）が含まれること。"""
    env.active_sprint()
    script = (
        "import time; from pathlib import Path; from caucus_race.runner import RunLock\n"
        f"with RunLock(Path({str(env.settings.run_dir)!r}), {P!r}).hold():\n"
        "    time.sleep(30)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        while RunLock(env.settings.run_dir, P).running() is None:
            assert time.monotonic() < deadline, "子プロセスがロックを取りませんでした"
            time.sleep(0.05)

        result = env.tools.stop_sprint(P)

        assert proc.wait(10) != 0
        assert result.pid == proc.pid
        assert "resume_sprint" in result.message
    finally:
        if proc.poll() is None:
            proc.kill()


# --- レビュー ---


def test_get_review_returns_markdown_and_saved_path(env: Env) -> None:
    """get_review はレビュー資料の Markdown 本文と保存先を返し、
    保存先のファイルの中身が本文と一致すること。"""
    sprint, story = env.review_sprint()

    result = env.tools.get_review(P)

    assert f"スプリント #{sprint.number} レビュー" in result.markdown
    assert f"{story.id}: {story.title}" in result.markdown
    assert Path(result.path).read_text(encoding="utf-8") == result.markdown


@pytest.mark.parametrize("with_story", [True, False])
def test_add_feedback_registers_feedback_with_optional_story_as_human(env: Env, with_story: bool) -> None:
    """add_feedback はレビュー中のスプリントにフィードバックを登録し（ストーリーの指定は任意）、
    人間の操作として記録されること。"""
    sprint, story = env.review_sprint()
    story_id = story.id if with_story else None

    result = env.tools.add_feedback(P, "件数も表示したい", story_id)

    [feedback] = env.repo.query(Feedback, sprint_id=sprint.id)
    assert (feedback.id, feedback.body, feedback.story_id) == (result.id, "件数も表示したい", story_id)
    assert [e.actor for e in env.events("feedback.added")] == [Actor.HUMAN]


def test_complete_review_closes_sprint_and_turns_feedback_into_backlog_story(env: Env) -> None:
    """complete_review でスプリントが終了し、フィードバックがバックログのストーリーになり、
    終了が人間の操作として記録されること。"""
    sprint, _ = env.review_sprint()
    env.tools.add_feedback(P, "件数も表示したい")

    result = env.tools.complete_review(P)

    assert env.repo.get(Sprint, sprint.id).status == SprintStatus.CLOSED
    assert result.status == SprintStatus.CLOSED
    backlog = [s for s in env.backlog.backlog(P) if s.source == StorySource.FEEDBACK]
    assert [s.title for s in backlog] == ["件数も表示したい"]
    assert [e.actor for e in env.events("sprint.closed")] == [Actor.HUMAN]
