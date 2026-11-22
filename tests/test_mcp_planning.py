"""MCP の起票・スプリント計画ツール（PlanningTools）とバックグラウンドの計画ジョブのテスト。

MCP の SDK を介さず、ツールの関数を直接呼ぶ。ツールの関数がエージェントに返す内容の境界になる。
別プロセスの起動（プロセス境界）だけを、同じ処理をスレッドで行う起動器に置き換える。
"""

import fcntl
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from injector import Binder, Injector, InstanceProvider, Module

from caucus_race.adapter.executor.fake import FakeExecutor, write_note
from caucus_race.adapter.notifier.console import RecordingNotifier
from caucus_race.adapter.planner.fake import FakePlanner
from caucus_race.container import AppModule
from caucus_race.domain.entities import ExecutionResult, Project, Spike, Sprint, Story, Task
from caucus_race.domain.errors import DomainError, InvalidTransitionError, NotFoundError
from caucus_race.domain.status import SprintStatus, StoryStatus
from caucus_race.mcp.planning_tools import PlanJobResult, PlanningTools, ToolFailure, call_tool
from caucus_race.orchestrator import Orchestrator
from caucus_race.plan_job import PlanJobs, launch_detached, run_plan_job
from caucus_race.port.executor import Brief
from caucus_race.port.planner import (
    PlannedStoryDraft,
    PlannerError,
    PlanningContext,
    SprintPlanDraft,
    TaskDraft,
)
from caucus_race.port.repository import Repository
from caucus_race.port.workspace import WorkspaceError
from caucus_race.settings import Settings
from caucus_race.usecase.ai_planning import AIPlanningService
from caucus_race.usecase.planning import PlanningService
from caucus_race.usecase.project import ProjectService
from caucus_race.usecase.review import ReviewService
from caucus_race.usecase.sprint import SprintService
from tests.conftest import FakeAdaptersModule

P = "demo"


class GatedPlanner(FakePlanner):
    """gate が開くまで計画を返さないプランナー（時間のかかる AI プランナーの代わり）。"""

    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()
        self.called = threading.Event()
        self.error: Exception | None = None

    def create_sprint_plan(self, context: PlanningContext) -> SprintPlanDraft:
        self.called.set()
        assert self.gate.wait(10), "テストが gate を開けませんでした"
        if self.error is not None:
            raise self.error
        return super().create_sprint_plan(context)


class ThreadLauncher:
    """別プロセスの代わりに、別スレッドで `caucus plan run` と同じ処理を行う。

    子プロセスと同じく、自分専用の Repository（別の Injector）を使い、ロックを持った記述子を引き継ぐ。
    `started=False` にすると、何もせずに終わる（結果を書く前に異常終了したプロセスの代わり）。
    `hold=True` にすると、計画を作らずにロックだけを持ち続ける（作成中のプロセスの代わり）。
    `release()` で手放す。
    """

    def __init__(self, make_child: Callable[[], Injector]) -> None:
        self.make_child = make_child
        self.threads: list[threading.Thread] = []
        self.started = True
        self.hold = False
        self.held: list[int] = []

    def __call__(self, argv: list[str], log_path: Path, lock_fd: int) -> None:
        if not self.started:
            return
        if self.hold:
            self.held.append(os.dup(lock_fd))
            return
        fd = os.dup(lock_fd)
        project = argv[-1]

        def run() -> None:
            try:
                child = self.make_child()
                run_plan_job(child.get(AIPlanningService), child.get(PlanJobs), project)
            finally:
                os.close(fd)

        thread = threading.Thread(target=run)
        thread.start()
        self.threads.append(thread)

    def join(self) -> None:
        for t in self.threads:
            t.join(10)

    def release(self) -> None:
        while self.held:
            os.close(self.held.pop())


class Env:
    def __init__(self, tmp_path: Path, planner: GatedPlanner) -> None:
        settings = Settings(data_dir=tmp_path / "data")
        notifier = RecordingNotifier()
        self.launcher = ThreadLauncher(self._make)
        self.jobs = PlanJobs(settings.run_dir, settings.log_dir, self.launcher)
        self._settings = settings
        self._notifier = notifier
        self._planner = planner
        self.executor = (
            FakeExecutor()
        )  # 子（計画ジョブ）とも共有し、テストからシナリオを差し替えられるようにする
        injector = self._make()
        self.injector = injector
        self.tools = injector.get(PlanningTools)
        self.repo: Repository = injector.get(Repository)  # type: ignore[type-abstract]
        self.projects = injector.get(ProjectService)
        self.planning = injector.get(PlanningService)
        self.ai = injector.get(AIPlanningService)
        self.sprints = injector.get(SprintService)
        self.orchestrator = injector.get(Orchestrator)
        self.review = injector.get(ReviewService)

    def _make(self) -> Injector:
        jobs = self.jobs

        class JobsModule(Module):
            def configure(self, binder: Binder) -> None:
                binder.bind(PlanJobs, to=InstanceProvider(jobs))

        return Injector(
            [
                AppModule(self._settings),
                FakeAdaptersModule(self.executor, self._notifier, self._planner),
                JobsModule(),
            ]
        )


@pytest.fixture
def planner() -> GatedPlanner:
    return GatedPlanner()


@pytest.fixture
def env(tmp_path: Path, git_repo: Path, planner: GatedPlanner) -> Iterator[Env]:
    e = Env(tmp_path, planner)
    e.projects.register(P, git_repo, max_stories_per_sprint=2, planner_config={"type": "claude"})
    yield e
    # 計画ジョブのスレッドとロックを残さない
    e.launcher.release()
    planner.gate.set()
    e.launcher.join()


# ルール違反のテストは「例外テスト」節（X1〜X16）に統合した


# --- 時間のかかる計画の作成 ---
# 結果（成功・失敗・異常終了・再試行）の確認は、組み合わせテスト G と状態遷移テストに統合した


def test_plan_sprint_returns_before_planner_finishes(env: Env, planner: GatedPlanner) -> None:
    """plan_sprint は、プランナーが計画を作っている最中（完了を待たずに）に running を返すこと。

    受け入れ条件「計画の作成に時間がかかっても、ツール呼び出しがタイムアウトで失敗しない」の中心。
    プランナーは gate が開くまで返らないので、plan_sprint が完了を待つと 10 秒後に失敗する。"""
    story = env.tools.add_story(P, "一覧画面")
    planner.plans.append(
        SprintPlanDraft(
            goal="記事を探せる",
            stories=[PlannedStoryDraft(story_id=story.id, title="", tasks=[TaskDraft(title="API")])],
        )
    )

    started = env.tools.plan_sprint(P)  # gate が閉じたままでも返る

    assert started.status == "running"
    assert planner.called.wait(5)  # プランナーは呼ばれて、まだ作成中
    assert env.tools.get_plan_status(P).status == "running"


def test_real_launcher_returns_immediately_keeps_lock_and_redirects_output(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """本物の起動器（launch_detached）は、子プロセスの終了を待たずにすぐ返り、
    ロックを子プロセスに引き継ぎ、子プロセスの出力をログに書く（MCP の stdio に混ぜない）こと。

    子プロセスは出力してから 3 秒眠る。起動器が子の終了を待つと、1 秒以内には返らない。"""
    lock_path = tmp_path / "x.plan.lock"
    log_path = tmp_path / "logs" / "x.log"
    child = [sys.executable, "-c", "import time; print('子の出力', flush=True); time.sleep(3)"]
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    started = time.monotonic()
    launch_detached(child, log_path, fd)
    elapsed = time.monotonic() - started
    os.close(fd)  # 起動側はロックを手放す。子プロセスが持ち続ける

    assert elapsed < 1.0, f"起動器が {elapsed:.1f} 秒待ちました"
    other = os.open(lock_path, os.O_RDONLY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)
    finally:
        os.close(other)
    deadline = time.monotonic() + 10
    while "子の出力" not in (log_path.read_text(encoding="utf-8") if log_path.exists() else ""):
        assert time.monotonic() < deadline, "子プロセスの出力がログに書かれませんでした"
        time.sleep(0.1)
    captured = capfd.readouterr()
    assert "子の出力" not in captured.out + captured.err


LONG_NAME = "記" * 40  # URL エンコードで 360 バイトになり、ファイル名の上限を超える名前


@pytest.mark.parametrize(
    "names",
    [
        pytest.param(["Foo", "foo", "FOO"], id="case-only"),
        pytest.param([LONG_NAME, LONG_NAME + "x", LONG_NAME[:-1]], id="long-same-prefix"),
        pytest.param(["a" * 199, "a" * 200, "a" * 201], id="truncation-edge"),
        pytest.param(["a/b", "a%2Fb", "A%2FB"], id="escape-lookalike"),
        pytest.param([".", "..", "%2E"], id="dots"),
    ],
)
def test_plan_job_file_names_are_distinct_and_creatable(tmp_path: Path, names: list[str]) -> None:
    """異なるプロジェクト名の計画ジョブのファイルは、大文字小文字を区別しないファイルシステムでも
    別々のファイルとして作られ、どれも 1 つのディレクトリの直下にできること（名前の長さ・記号によらない）。"""
    for name in names:
        jobs = PlanJobs(tmp_path / "run", tmp_path / "logs", lambda argv, log, fd: None)
        jobs.start(name)  # 起動器は何もしない（ジョブは異常終了の扱いになる）
    files = sorted(p for p in (tmp_path / "run").iterdir() if p.name.endswith(".plan.json"))
    assert len(files) == len(names), [p.name for p in files]
    for name in names:
        state = PlanJobs(tmp_path / "run", tmp_path / "logs").status(name)
        assert state is not None and state.project == name  # 他の名前の状態と混ざらない


# --- 正常系: 人手の計画 ---


def test_manual_planning_flow_builds_sprint(env: Env) -> None:
    """スプリントの作成・ストーリーの追加と除外・タスクとスパイクの追加の結果が、スプリントの内容として返ること。"""
    a = env.tools.add_story(P, "一覧画面")
    b = env.tools.add_story(P, "検索")
    env.tools.create_sprint(P, "記事を探せる")
    env.tools.add_story_to_sprint(P, a.id)
    env.tools.add_story_to_sprint(P, b.id)
    env.tools.add_task(a.id, "一覧 API")
    spike = env.tools.add_spike(a.id, "ページングは必要か", options=["必要", "不要"], recommendation="必要")

    sprint = env.tools.remove_story_from_sprint(P, b.id)

    assert sprint.goal == "記事を探せる"
    assert sprint.status == "planning"
    assert [s.id for s in sprint.stories] == [a.id]
    assert sprint.stories[0].tasks == ["一覧 API"]
    assert sprint.stories[0].open_spikes == [f"{spike.id}: ページングは必要か"]
    assert env.repo.get(Story, b.id).status == StoryStatus.BACKLOG


# =====================================================================
# 組み合わせテスト（設計表: docs/test-design/20261123-caucus-sprint-1.md の「## 組み合わせテスト」）
# =====================================================================

OMIT = object()  # 省略可能な引数を渡さないことを表す
OTHER = "other"
SPRINT_STATES = ["none", "planning", "approved", "active", "review", "closed"]


def kwargs_of(**candidates: object) -> dict[str, object]:
    return {k: v for k, v in candidates.items() if v is not OMIT}


def blocked(brief: Brief, workdir: Path) -> ExecutionResult:
    return ExecutionResult(status="blocked", blocked_reason="判断が必要")


def errored(brief: Brief, workdir: Path) -> ExecutionResult:
    return ExecutionResult(status="error", error="実行器の故障")


def advance_planning_sprint(env: Env, state: str, project: str = P) -> None:
    """planning のスプリントを state（planning / approved / active / review / closed）まで進める。"""
    if state == "planning":
        return
    env.planning.approve(project)
    if state == "approved":
        return
    sprint = env.sprints.start(project)
    if state == "active":
        return
    env.orchestrator.run(sprint.id)
    if state == "review":
        return
    env.review.close(project)


def put_sprint_in(env: Env, state: str, outcome: str = "done", project: str = P) -> str | None:
    """スプリントの事前状態 S を作る。スプリントに入れたストーリーの ID を返す（none なら None）。

    outcome は実行したときのストーリーの結果（done / failed / suspended）。review 以降で効く。
    """
    if state == "none":
        return None
    story = env.tools.add_story(project, "既存のストーリー")
    env.tools.create_sprint(project, "既存のゴール")
    env.tools.add_story_to_sprint(project, story.id)
    env.executor.default = {"done": write_note, "failed": errored, "suspended": blocked}[outcome]
    advance_planning_sprint(env, state, project)
    env.executor.default = write_note
    return story.id


def register_other(env: Env, git_repo: Path) -> None:
    env.projects.register(OTHER, git_repo, max_stories_per_sprint=2, planner_config={"type": "claude"})


def hold_plan_job(env: Env, project: str = P) -> None:
    """計画ジョブ J を running にする（ロックを持ち続けるだけで計画は作らない）。"""
    env.launcher.hold = True
    env.jobs.start(project)
    env.launcher.hold = False


def story_snapshot(env: Env) -> list[tuple[str, str, str | None]]:
    """全ストーリーの（ID, 状態, 所属スプリント）。拒否された操作で何も変わらないことの確認に使う。"""
    return sorted((s.id, s.status, s.sprint_id) for s in env.repo.query(Story))


# --- A. add_story ---


@pytest.mark.parametrize(
    ("description", "expected_description"),
    [
        pytest.param(OMIT, "", id="desc-omitted"),
        pytest.param("記事の一覧。\n二行目", "記事の一覧。\n二行目", id="desc-multiline-non-ascii"),
    ],
)
@pytest.mark.parametrize(
    ("criteria", "expected_criteria"),
    [
        pytest.param(OMIT, [], id="criteria-omitted"),
        pytest.param(None, [], id="criteria-none"),
        pytest.param([], [], id="criteria-empty"),
        pytest.param(["新しい順に並ぶ"], ["新しい順に並ぶ"], id="criteria-one"),
        pytest.param(
            ["新しい順に並ぶ", "20 件ずつ\n表示する"],
            ["新しい順に並ぶ", "20 件ずつ\n表示する"],
            id="criteria-many-multiline",
        ),
    ],
)
def test_combo_add_story_stores_given_fields_in_backlog(
    env: Env, criteria: object, expected_criteria: list[str], description: object, expected_description: str
) -> None:
    """A1–A10: 存在するプロジェクトに起票したとき、受け入れ条件と説明が
    渡したとおり（省略・None・空は空）でバックログに入ること。"""
    result = env.tools.add_story(
        P, "一覧画面", **kwargs_of(description=description, acceptance_criteria=criteria)
    )

    story = env.repo.get(Story, result.id)
    assert (story.status, story.sprint_id) == (StoryStatus.BACKLOG, None)
    assert story.acceptance_criteria == expected_criteria
    assert story.description == expected_description
    assert (result.acceptance_criteria, result.description) == (expected_criteria, expected_description)


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({}, id="A11-missing-project-all-omitted"),
        pytest.param(
            {"description": "a\nb", "acceptance_criteria": ["x", "y"]}, id="A12-missing-project-all-given"
        ),
    ],
)
def test_combo_add_story_to_missing_project_fails_without_creating(env: Env, kwargs: dict) -> None:
    """A11–A12: 存在しないプロジェクトに起票したとき、例外になりストーリーは作られないこと。"""
    with pytest.raises(DomainError):
        env.tools.add_story("nope", "一覧画面", **kwargs)
    assert env.repo.query(Story) == []


def test_combo_add_story_is_allowed_while_ai_is_planning(env: Env) -> None:
    """A13: 計画ジョブの作成中でも、バックログへの起票はできること（起票は計画の変更ではない）。"""
    hold_plan_job(env)
    result = env.tools.add_story(P, "一覧画面", acceptance_criteria=["新しい順に並ぶ"])
    assert env.repo.get(Story, result.id).status == StoryStatus.BACKLOG


# --- B. create_sprint ---


@pytest.mark.parametrize("job", ["idle", "running"])
@pytest.mark.parametrize("state", SPRINT_STATES)
def test_combo_create_sprint_by_sprint_state_and_job(env: Env, state: str, job: str) -> None:
    """B1–B12: スプリントは、未終了のスプリントがなく計画ジョブも実行中でないときだけ作成され、
    番号は直前のスプリントの次になること。それ以外は例外で、スプリントは増えないこと。"""
    put_sprint_in(env, state)
    if job == "running":
        hold_plan_job(env)
    before = len(env.repo.query(Sprint))

    if state in ("none", "closed") and job == "idle":
        result = env.tools.create_sprint(P, "記事を探せる")
        assert result.number == before + 1
        assert (result.goal, result.status, result.stories) == ("記事を探せる", "planning", [])
    else:
        with pytest.raises(DomainError):
            env.tools.create_sprint(P, "記事を探せる")
        assert len(env.repo.query(Sprint)) == before


def test_combo_create_sprint_keeps_multiline_goal(env: Env) -> None:
    """B13: 改行と非 ASCII を含むゴールが、そのまま保存されること。"""
    goal = "記事を探せる\n（検索と一覧）"
    result = env.tools.create_sprint(P, goal)
    assert result.goal == goal
    assert env.repo.query(Sprint)[0].goal == goal


def test_combo_create_sprint_for_missing_project_fails(env: Env) -> None:
    """B14: 存在しないプロジェクトにスプリントを作成すると、例外になりスプリントは作られないこと。"""
    with pytest.raises(DomainError):
        env.tools.create_sprint("nope", "記事を探せる")
    assert env.repo.query(Sprint) == []


# --- C. add_story_to_sprint ---


def _target_story(env: Env, kind: str, git_repo: Path, in_sprint: str | None) -> str:
    match kind:
        case "same-backlog":
            return env.tools.add_story(P, "対象").id
        case "other-project":
            register_other(env, git_repo)
            return env.tools.add_story(OTHER, "別プロジェクトの対象").id
        case "already-in":
            assert in_sprint is not None
            return in_sprint
        case "done-in-closed":
            # 終了したスプリントで完了したストーリー。新しい planning のスプリントに追加しようとする
            done = put_sprint_in(env, "closed")
            assert done is not None
            env.tools.create_sprint(P, "次のゴール")
            return done
        case "missing":
            return "ST-999"
    raise AssertionError(kind)


@pytest.mark.parametrize(
    ("project", "state", "target", "capacity", "job", "ok"),
    [
        pytest.param(P, "none", "same-backlog", "below", "idle", False, id="C1-none"),
        pytest.param(P, "planning", "same-backlog", "below", "idle", True, id="C2-planning-ok"),
        pytest.param(P, "approved", "same-backlog", "below", "idle", False, id="C3-approved"),
        pytest.param(P, "active", "same-backlog", "below", "idle", False, id="C4-active"),
        pytest.param(P, "review", "same-backlog", "below", "idle", False, id="C5-review"),
        pytest.param(P, "closed", "same-backlog", "below", "idle", False, id="C6-closed"),
        pytest.param(P, "planning", "same-backlog", "full", "idle", False, id="C7-full"),
        pytest.param(P, "planning", "other-project", "below", "idle", False, id="C8-other-project"),
        pytest.param(P, "planning", "other-project", "full", "idle", False, id="C9-other-project-full"),
        pytest.param(P, "planning", "already-in", "below", "idle", False, id="C10-already-in"),
        pytest.param(P, "planning", "already-in", "full", "idle", False, id="C11-already-in-full"),
        pytest.param(P, "planning", "missing", "below", "idle", False, id="C12-missing"),
        pytest.param(P, "planning", "missing", "full", "idle", False, id="C13-missing-full"),
        pytest.param(P, "planning", "same-backlog", "below", "running", False, id="C14-job-running"),
        pytest.param("nope", "planning", "same-backlog", "below", "idle", False, id="C15-missing-project"),
        pytest.param(P, "none", "done-in-closed", "below", "idle", False, id="C16-done-in-closed-sprint"),
    ],
)
def test_combo_add_story_to_sprint(
    env: Env, git_repo: Path, project: str, state: str, target: str, capacity: str, job: str, ok: bool
) -> None:
    """C1–C16: 存在するプロジェクトの上限未満の planning のスプリントに、同じプロジェクトのバックログの
    ストーリーを、計画ジョブの実行中でないときだけ追加できること。それ以外は例外で、どのストーリーも変わらないこと。"""
    in_sprint = put_sprint_in(env, state)
    if capacity == "full":  # 上限 2 件。put_sprint_in で 1 件入っている
        env.tools.add_story_to_sprint(P, env.tools.add_story(P, "埋める").id)
    story_id = _target_story(env, target, git_repo, in_sprint)
    if job == "running":
        hold_plan_job(env)
    before = story_snapshot(env)

    if ok:
        result = env.tools.add_story_to_sprint(project, story_id)
        assert story_id in [s.id for s in result.stories]
        assert env.repo.get(Story, story_id).status == StoryStatus.PLANNED
    else:
        with pytest.raises(DomainError):
            env.tools.add_story_to_sprint(project, story_id)
        assert story_snapshot(env) == before


# --- D. remove_story_from_sprint ---


@pytest.mark.parametrize(
    ("state", "target"),
    [
        pytest.param(state, target, id=f"{state}-{target}")
        for state in SPRINT_STATES
        for target in ["in-sprint", "backlog", "missing", "other-project-in-sprint"]
        # スプリントがないため「スプリントにある」ストーリーは作れない（設計表で除外）
        if not (state == "none" and target == "in-sprint")
    ],
)
def test_combo_remove_story_from_sprint_by_state_and_target(
    env: Env, git_repo: Path, state: str, target: str
) -> None:
    """D1–D23: planning のスプリントにある、そのプロジェクトのストーリーだけを外せること。
    それ以外（承認後以降・終了済み・バックログ・存在しない・別のプロジェクトのスプリント）は例外で、
    どのストーリーも変わらないこと。"""
    in_sprint = put_sprint_in(env, state)
    match target:
        case "in-sprint":
            assert in_sprint is not None
            story_id = in_sprint
        case "backlog":
            story_id = env.tools.add_story(P, "バックログのまま").id
        case "missing":
            story_id = "ST-999"
        case _:
            register_other(env, git_repo)
            story_id = put_sprint_in(env, "planning", project=OTHER) or ""
    before = story_snapshot(env)

    if state == "planning" and target == "in-sprint":
        result = env.tools.remove_story_from_sprint(P, story_id)
        assert story_id not in [s.id for s in result.stories]
        story = env.repo.get(Story, story_id)
        assert (story.status, story.sprint_id) == (StoryStatus.BACKLOG, None)
    else:
        with pytest.raises(DomainError):
            env.tools.remove_story_from_sprint(P, story_id)
        assert story_snapshot(env) == before


@pytest.mark.parametrize(
    ("project", "job"),
    [
        pytest.param(P, "running", id="D24-job-running"),
        pytest.param("nope", "idle", id="D25-missing-project-with-real-story"),
    ],
)
def test_combo_remove_story_from_sprint_rejected_cases(env: Env, project: str, job: str) -> None:
    """D24–D25: planning のスプリントにあるストーリーでも、計画ジョブの実行中や、存在しないプロジェクト名を
    指定したときは例外になり、ストーリーはスプリントに残ること。"""
    story_id = put_sprint_in(env, "planning")
    assert story_id is not None
    if job == "running":
        hold_plan_job(env)
    before = story_snapshot(env)

    with pytest.raises(DomainError):
        env.tools.remove_story_from_sprint(project, story_id)
    assert story_snapshot(env) == before


# --- E. add_task / add_spike ---


def _add_item(env: Env, tool: str, story_id: str) -> str:
    if tool == "task":
        return env.tools.add_task(story_id, "一覧 API").story_id
    return env.tools.add_spike(story_id, "ページングは必要か").story_id


def _count_items(env: Env, tool: str) -> int:
    return len(env.repo.query(Task if tool == "task" else Spike))


def _add_item_while_running(env: Env, tool: str) -> tuple[str, Exception | None]:
    """実行中（running）のストーリーにツールを呼ぶ。実行器の中から呼んで、そのときの結果を返す。"""
    story_id = put_sprint_in(env, "active")
    assert story_id is not None
    outcome: list[Exception | None] = []

    def scenario(brief: Brief, workdir: Path) -> ExecutionResult:
        assert env.repo.get(Story, brief.story_id).status == StoryStatus.RUNNING
        try:
            _add_item(env, tool, brief.story_id)
            outcome.append(None)
        except DomainError as e:
            outcome.append(e)
        return write_note(brief, workdir)

    env.executor.scenarios[story_id] = scenario
    sprint_id = env.repo.get(Story, story_id).sprint_id
    assert sprint_id is not None
    env.orchestrator.run(sprint_id)
    return story_id, outcome[0]


@pytest.mark.parametrize(
    ("story_state", "ok"),
    [
        pytest.param("backlog", True, id="backlog"),
        pytest.param("planning", True, id="in-planning-sprint"),
        pytest.param("approved", False, id="in-approved-sprint"),
        pytest.param("active", False, id="queued-in-active-sprint"),
        pytest.param("running", False, id="running"),
        pytest.param("review-done", False, id="done-in-review"),
        pytest.param("review-failed", False, id="failed-in-review"),
        pytest.param("review-suspended", False, id="suspended-in-review"),
        pytest.param("closed-done", False, id="done-in-closed"),
        pytest.param("missing", False, id="missing"),
    ],
)
@pytest.mark.parametrize("tool", ["task", "spike"])
def test_combo_add_task_or_spike_by_story_state(env: Env, tool: str, story_state: str, ok: bool) -> None:
    """E1–E20: タスクとスパイクは、バックログか planning のスプリントのストーリーにだけ追加でき、
    それ以外（承認後・実行待ち・実行中・完了・失敗・中断・終了済み・存在しない）は例外で何も追加されないこと。"""
    if story_state == "running":
        before = _count_items(env, tool)
        _, error = _add_item_while_running(env, tool)
        assert error is not None
        assert _count_items(env, tool) == before
        return
    match story_state:
        case "backlog":
            story_id = env.tools.add_story(P, "対象").id
        case "missing":
            story_id = "ST-999"
        case _:
            sprint_state, _, outcome = story_state.partition("-")
            story_id = put_sprint_in(env, sprint_state, outcome or "done")
            assert story_id is not None
            expected = {"done": StoryStatus.DONE, "failed": StoryStatus.FAILED}
            if outcome in expected:  # 事前状態が意図どおりかを確かめる
                assert env.repo.get(Story, story_id).status == expected[outcome]
    before = _count_items(env, tool)

    if ok:
        assert _add_item(env, tool, story_id) == story_id
        assert _count_items(env, tool) == before + 1
    else:
        with pytest.raises(DomainError):
            _add_item(env, tool, story_id)
        assert _count_items(env, tool) == before


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        pytest.param(OMIT, "", id="desc-omitted"),
        pytest.param("一覧を返す。\n件数も返す", "一覧を返す。\n件数も返す", id="desc-multiline-non-ascii"),
    ],
)
def test_combo_add_task_description(env: Env, description: object, expected: str) -> None:
    """E21–E22: タスクの説明は、省略なら空、指定すれば改行を含めてそのまま保存されること。"""
    story_id = env.tools.add_story(P, "対象").id
    result = env.tools.add_task(story_id, "一覧 API", **kwargs_of(description=description))
    assert result.description == expected
    assert [t.description for t in env.repo.query(Task, story_id=story_id)] == [expected]


@pytest.mark.parametrize(
    ("context", "expected_context"),
    [pytest.param(OMIT, "", id="ctx-omitted"), pytest.param("背景\n二行目", "背景\n二行目", id="ctx-given")],
)
@pytest.mark.parametrize(
    ("recommendation", "expected_recommendation"),
    [
        pytest.param(OMIT, None, id="rec-omitted"),
        pytest.param(None, None, id="rec-none"),
        pytest.param("必要", "必要", id="rec-given"),
    ],
)
@pytest.mark.parametrize(
    ("options", "expected_options"),
    [
        pytest.param(OMIT, [], id="options-omitted"),
        pytest.param(None, [], id="options-none"),
        pytest.param([], [], id="options-empty"),
        pytest.param(["必要"], ["必要"], id="options-one"),
        pytest.param(["必要", "不要\n（後で）"], ["必要", "不要\n（後で）"], id="options-many"),
    ],
)
def test_combo_add_spike_optional_arguments(
    env: Env,
    options: object,
    expected_options: list[str],
    recommendation: object,
    expected_recommendation: str | None,
    context: object,
    expected_context: str,
) -> None:
    """E23–E52: スパイクの選択肢・推奨・背景は、省略または None なら空（推奨は None）、
    指定すれば渡したとおりに保存されること。"""
    story_id = env.tools.add_story(P, "対象").id
    result = env.tools.add_spike(
        story_id,
        "ページングは必要か",
        **kwargs_of(options=options, context=context, recommendation=recommendation),
    )
    expected = (expected_options, expected_context, expected_recommendation)
    spike = env.repo.get(Spike, result.id)
    assert (spike.options, spike.context, spike.recommendation) == expected
    assert (result.options, result.context, result.recommendation) == expected


# --- H. 計画ジョブのロックはプロジェクトごと ---


def _edit_applied(env: Env, tool: str, project: str) -> bool:
    """_prepare_edit の操作が実際に反映されたか。"""
    project_id = env.repo.query(Project, name=project)[0].id
    sprints = env.repo.query(Sprint, project_id=project_id)
    stories = env.repo.query(Story, project_id=project_id)
    match tool:
        case "create_sprint":
            return [s.goal for s in sprints] == ["ゴール"]
        case "add_story_to_sprint":
            return [s.title for s in stories if s.sprint_id] == ["追加する"]
        case "remove_story_from_sprint":
            return all(s.sprint_id is None for s in stories)
        case "add_task":
            return [t.title for s in stories for t in env.repo.query(Task, story_id=s.id)] == ["タスク"]
        case "add_spike":
            return [x.question for s in stories for x in env.repo.query(Spike, story_id=s.id)] == ["質問"]
    raise AssertionError(tool)


def _prepare_edit(env: Env, tool: str, project: str) -> Callable[[], object]:
    """project に対する計画変更の操作を、実行できる事前状態にして返す。"""
    match tool:
        case "create_sprint":
            return lambda: env.tools.create_sprint(project, "ゴール")
        case "add_story_to_sprint":
            story = env.tools.add_story(project, "追加する")
            env.tools.create_sprint(project, "ゴール")
            return lambda: env.tools.add_story_to_sprint(project, story.id)
        case "remove_story_from_sprint":
            story_id = put_sprint_in(env, "planning", project=project)
            assert story_id is not None
            return lambda: env.tools.remove_story_from_sprint(project, story_id)
        case "add_task":
            story = env.tools.add_story(project, "対象")
            return lambda: env.tools.add_task(story.id, "タスク")
        case "add_spike":
            story = env.tools.add_story(project, "対象")
            return lambda: env.tools.add_spike(story.id, "質問")
    raise AssertionError(tool)


@pytest.mark.parametrize("running_on", ["same", "other"])
@pytest.mark.parametrize("project", [P, OTHER])
@pytest.mark.parametrize(
    "tool", ["create_sprint", "add_story_to_sprint", "remove_story_from_sprint", "add_task", "add_spike"]
)
def test_combo_planning_lock_is_per_project(
    env: Env, git_repo: Path, tool: str, project: str, running_on: str
) -> None:
    """H1–H20: 計画変更の操作は、操作するプロジェクト（タスク・スパイクはストーリーのプロジェクト）で
    計画ジョブが実行中のときだけ拒否され、別のプロジェクトのジョブには影響されないこと。"""
    register_other(env, git_repo)
    operation = _prepare_edit(env, tool, project)
    hold_plan_job(env, project if running_on == "same" else ({P, OTHER} - {project}).pop())
    before = story_snapshot(env)

    if running_on == "same":
        with pytest.raises(DomainError, match="計画を作成中のため変更できません"):
            operation()
        assert story_snapshot(env) == before
        assert not _edit_applied(env, tool, project)
    else:
        operation()
        assert _edit_applied(env, tool, project)


# --- F. plan_sprint ---


def _run_job_to_end(env: Env, planner: GatedPlanner, *, error: Exception | None, draft_story: bool) -> None:
    """計画ジョブを最後まで実行させる（J=failed / succeeded を作る）。"""
    planner.error = error
    if draft_story:
        story = env.tools.add_story(P, "計画対象")
        planner.plans.append(
            SprintPlanDraft(goal="記事を探せる", stories=[PlannedStoryDraft(story_id=story.id, title="")])
        )
    planner.gate.set()
    env.tools.plan_sprint(P)
    env.launcher.join()
    planner.error = None


@pytest.mark.parametrize(
    ("state", "job"),
    [
        pytest.param(state, job, id=f"{state}-{job}")
        for state in SPRINT_STATES
        for job in ["idle", "running", "failed", "succeeded"]
        # 計画の成功はスプリントを作るため「スプリントなし」とは両立しない（設計表で除外）
        if not (state == "none" and job == "succeeded")
    ],
)
def test_combo_plan_sprint_by_sprint_state_and_job(
    env: Env, planner: GatedPlanner, state: str, job: str
) -> None:
    """F1–F23: AI プランナーが設定されたプロジェクトで、計画を変更できる状態
    （スプリントなし・planning・終了済み）かつ計画ジョブが実行中でないときだけ、計画の作成が始まり
    running が返ること。それ以外は例外で、ジョブの状態は変わらないこと。"""
    if job == "failed":
        _run_job_to_end(env, planner, error=PlannerError("前回の失敗"), draft_story=False)
        assert env.tools.get_plan_status(P).status == "failed"
    if job == "succeeded":
        _run_job_to_end(env, planner, error=None, draft_story=True)
        assert env.tools.get_plan_status(P).status == "succeeded"
        advance_planning_sprint(env, state)
    else:
        put_sprint_in(env, state)
    if job == "running":
        hold_plan_job(env)
    before = env.jobs.status(P)

    env.launcher.hold = True  # 始まったジョブは作成中のままにする
    if state in ("none", "planning", "closed") and job != "running":
        assert env.tools.plan_sprint(P).status == "running"
        assert env.tools.get_plan_status(P).status == "running"
    else:
        with pytest.raises(DomainError):
            env.tools.plan_sprint(P)
        assert env.jobs.status(P) == before


@pytest.mark.parametrize(
    ("planner_config", "name", "job", "ok"),
    [
        pytest.param({"type": "manual"}, "manual-prj", "idle", False, id="F24-manual"),
        pytest.param({"model": "opus"}, "default-prj", "idle", False, id="F25-type-omitted"),
        pytest.param(None, "nope", "idle", False, id="F26-missing-project"),
        pytest.param({"type": "claude"}, "記事 アプリ", "idle", True, id="F27-non-ascii-name"),
        pytest.param({"type": "manual"}, "manual-prj", "running", False, id="F28-manual-and-running"),
    ],
)
def test_combo_plan_sprint_by_project(
    env: Env, git_repo: Path, planner_config: dict | None, name: str, job: str, ok: bool
) -> None:
    """F24–F28: AI プランナーが設定されていない（type の省略を含む）・存在しないプロジェクトでは例外になり
    ジョブは始まらず、非 ASCII・空白を含む名前のプロジェクトでも計画の作成を始められること。"""
    if planner_config is not None:
        env.projects.register(name, git_repo, planner_config=planner_config)
    if job == "running":
        hold_plan_job(env, name)
    before = env.jobs.status(name)

    env.launcher.hold = True
    if ok:
        assert env.tools.plan_sprint(name).status == "running"
        assert env.tools.get_plan_status(name).status == "running"
        assert env.tools.get_plan_status(P).status == "not_started"  # 他のプロジェクトのジョブは始まらない
    else:
        with pytest.raises(DomainError):
            env.tools.plan_sprint(name)
        assert env.jobs.status(name) == before


def test_combo_detached_plan_process_receives_non_ascii_project_name(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F29: 非 ASCII・空白を含むプロジェクト名でも、別プロセスに名前が正しく渡り、
    そのプロジェクトの結果が書かれること。

    名前が壊れれば「プロジェクトが見つかりません」になる。本物の AI プランナーは呼べないため、
    AI プランナー未設定のプロジェクトで「そのプロジェクトのプランナーのエラー」が書かれることで確かめる。
    """
    name = "記事 アプリ"
    home = tmp_path / "home"
    monkeypatch.setenv("CAUCUS_HOME", str(home))
    settings = Settings(data_dir=home)
    Injector([AppModule(settings)]).get(ProjectService).register(name, git_repo)
    jobs = PlanJobs(settings.run_dir, settings.log_dir, launch_detached)

    jobs.start(name)

    deadline = time.monotonic() + 30
    while (state := jobs.status(name)) is not None and state.status == "running":
        assert time.monotonic() < deadline, "計画のプロセスが終わりませんでした"
        time.sleep(0.2)
    assert state is not None and state.status == "failed"
    assert state.error is not None and "AI プランナーが設定されていません" in state.error
    # 失敗の原因を調べられるよう、ログの場所が返り、そこに理由が書かれている
    assert state.log_path is not None and state.log_path.parent == settings.log_dir
    assert "AI プランナーが設定されていません" in state.log_path.read_text(encoding="utf-8")


# --- G. get_plan_status と run_plan_job の結果 ---


def test_combo_get_plan_status_of_missing_project_fails(env: Env) -> None:
    """G1: 存在しないプロジェクトの状態を問い合わせると例外になること。"""
    with pytest.raises(DomainError):
        env.tools.get_plan_status("nope")


@pytest.mark.parametrize(
    "job",
    [
        pytest.param("never", id="G2-never"),
        pytest.param("running", id="G3-running"),
        pytest.param("succeeded", id="G4-succeeded"),
        pytest.param("planner-error", id="G5-planner-error"),
        pytest.param("unexpected-error", id="G6-unexpected-error"),
        pytest.param("died", id="G7-died-without-result"),
        pytest.param("domain-error", id="G8-approved-before-child-runs"),
        pytest.param("workspace-error", id="G9-workspace-error"),
    ],
)
def test_combo_get_plan_status_by_job_outcome(env: Env, planner: GatedPlanner, job: str) -> None:
    """G2–G9: 計画ジョブの結果ごとに、get_plan_status が対応する状態を返し、
    成功なら計画の内容を、失敗なら理由を含むこと（running のまま残らないこと）。"""
    story_id = ""
    match job:
        case "running":
            hold_plan_job(env)
        case "succeeded":
            story_id = env.tools.add_story(P, "計画対象").id
            planner.plans.append(
                SprintPlanDraft(goal="記事を探せる", stories=[PlannedStoryDraft(story_id=story_id, title="")])
            )
        case "planner-error":
            planner.error = PlannerError("claude が失敗しました")
        case "unexpected-error":
            planner.error = RuntimeError("予期しない故障")
        case "workspace-error":
            planner.error = WorkspaceError("git が失敗しました")
        case "died":
            env.launcher.started = False
    if job == "domain-error":
        # plan_sprint の事前チェックの後、子プロセスが動き出す前に人間が承認した競合
        put_sprint_in(env, "approved")
        planner.gate.set()
        env.jobs.start(P)
        env.launcher.join()
    elif job not in ("never", "running"):
        planner.gate.set()
        env.tools.plan_sprint(P)
        env.launcher.join()

    status = env.tools.get_plan_status(P)

    expected_status = {"never": "not_started", "running": "running", "succeeded": "succeeded"}.get(
        job, "failed"
    )
    assert status.status == expected_status
    if job == "succeeded":
        assert (status.sprint_number, status.goal, status.story_ids, status.error) == (
            1,
            "記事を探せる",
            [story_id],
            None,
        )
    if expected_status == "failed":
        assert status.error
    expected_reason = {
        "planner-error": "claude が失敗しました",
        "unexpected-error": "予期しない故障",
        "workspace-error": "git が失敗しました",
        "domain-error": "終了していません",
    }
    if job in expected_reason:
        assert expected_reason[job] in (status.error or "")


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        pytest.param(None, "succeeded", id="G10-run-directly-succeeded"),
        pytest.param(PlannerError("claude が失敗しました"), "failed", id="G11-run-directly-failed"),
    ],
)
def test_combo_plan_run_without_prior_start_records_result(
    env: Env, planner: GatedPlanner, error: Exception | None, expected_status: str
) -> None:
    """G10–G11: plan_sprint を経ずに計画の処理（caucus plan run）を直接実行したときも、
    結果が get_plan_status で読めること。"""
    story_id = env.tools.add_story(P, "計画対象").id
    planner.plans.append(
        SprintPlanDraft(goal="記事を探せる", stories=[PlannedStoryDraft(story_id=story_id, title="")])
    )
    planner.error = error
    planner.gate.set()
    assert env.tools.get_plan_status(P).status == "not_started"

    run_plan_job(env.ai, env.jobs, P)

    status = env.tools.get_plan_status(P)
    assert status.status == expected_status
    if expected_status == "succeeded":
        assert (status.sprint_number, status.story_ids) == (1, [story_id])
    else:
        assert "claude が失敗しました" in (status.error or "")


# =====================================================================
# 境界値テスト（設計表: docs/test-design/20261123-caucus-sprint-1.md の「## 境界値テスト」）
# =====================================================================


def _fill_sprint(env: Env, project: str, count: int) -> None:
    """project に planning のスプリントを作り、ストーリーを count 件入れる。"""
    env.tools.create_sprint(project, "上限の確認")
    for i in range(count):
        env.tools.add_story_to_sprint(project, env.tools.add_story(project, f"既存 {i + 1}").id)


def _sprint_size(env: Env, project: str) -> int:
    """project の最新のスプリントに入っているストーリーの件数。"""
    project_id = env.repo.query(Project, name=project)[0].id
    sprint = max(env.repo.query(Sprint, project_id=project_id), key=lambda s: s.number)
    return len(env.repo.query(Story, sprint_id=sprint.id))


@pytest.mark.parametrize(
    ("limit", "existing", "ok"),
    [
        pytest.param(1, 0, True, id="BV1-limit1-on"),
        pytest.param(1, 1, False, id="BV2-limit1-off"),
        pytest.param(5, 4, True, id="BV3-limit5-default-on"),
        pytest.param(5, 5, False, id="BV4-limit5-default-off"),
    ],
)
def test_boundary_sprint_story_limit(env: Env, git_repo: Path, limit: int, existing: int, ok: bool) -> None:
    """BV1–BV4: スプリントのストーリー数が上限 N 未満（N − 1 件）なら追加できて N 件になり、
    N 件に達していれば例外になり件数は変わらないこと。"""
    name = f"limit{limit}"
    if limit == 5:
        env.projects.register(name, git_repo)  # 既定値の上限で登録する
    else:
        env.projects.register(name, git_repo, max_stories_per_sprint=limit)
    _fill_sprint(env, name, existing)
    target = env.tools.add_story(name, "追加する").id

    if ok:
        result = env.tools.add_story_to_sprint(name, target)
        assert len(result.stories) == limit
        assert target in [s.id for s in result.stories]
    else:
        with pytest.raises(DomainError, match="上限"):
            env.tools.add_story_to_sprint(name, target)
        assert _sprint_size(env, name) == existing
        assert env.repo.get(Story, target).status == StoryStatus.BACKLOG


def test_boundary_sprint_story_limit_frees_slot_after_removal(env: Env, git_repo: Path) -> None:
    """BV5: 上限（既定 5 件）に達したスプリントから 1 件外すと、もう 1 件追加できて再び 5 件になること。"""
    env.projects.register("limit5", git_repo)
    _fill_sprint(env, "limit5", 5)
    removed = env.repo.query(Story, project_id=env.repo.query(Project, name="limit5")[0].id)[0].id
    env.tools.remove_story_from_sprint("limit5", removed)
    target = env.tools.add_story("limit5", "追加する").id

    result = env.tools.add_story_to_sprint("limit5", target)

    assert len(result.stories) == 5
    assert removed not in [s.id for s in result.stories]


@pytest.mark.parametrize(
    ("draft", "expected"),
    [
        # 計画の並びは、あえて優先度（起票順 s1 → s2 → s3）と逆にして「計画の先頭から」を確かめる
        pytest.param([], None, id="BV6-plan-count-0-lower-edge"),
        pytest.param(["missing"], None, id="BV6b-plan-count-1-but-adopted-0"),
        pytest.param(["s3"], {"s3"}, id="BV7-plan-count-1"),
        pytest.param(["s3", "s1"], {"s3", "s1"}, id="BV8-plan-count-limit-on"),
        pytest.param(["s3", "s1", "s2"], {"s3", "s1"}, id="BV9-plan-count-limit-off-head-of-plan"),
    ],
)
def test_boundary_ai_plan_story_count(
    env: Env, planner: GatedPlanner, draft: list[str], expected: set[str] | None
) -> None:
    """BV6–BV9: AI の計画のストーリーが上限（2 件）以内ならすべて、上限を超えれば計画の先頭から上限までが
    スプリントに入り、get_plan_status の story_ids と一致すること（優先度の順ではない）。
    採用できるストーリーが 0 件なら、スプリントは作られず何も反映されないこと（docs/design.md）。"""
    ids = {key: env.tools.add_story(P, f"候補 {key}").id for key in ("s1", "s2", "s3")}
    ids["missing"] = "ST-999"
    planner.plans.append(
        SprintPlanDraft(
            goal="記事を探せる", stories=[PlannedStoryDraft(story_id=ids[k], title="") for k in draft]
        )
    )
    planner.gate.set()
    env.tools.plan_sprint(P)
    env.launcher.join()

    status = env.tools.get_plan_status(P)
    if expected is None:
        # 0 件のときのジョブの状態（failed か succeeded か）は仕様不明。終わっていることだけを確かめる
        assert status.status != "running"
        assert env.repo.query(Sprint) == []
        assert all(env.repo.get(Story, ids[k]).status == StoryStatus.BACKLOG for k in ("s1", "s2", "s3"))
        return
    expected_ids = {ids[k] for k in expected}
    assert status.status == "succeeded"
    assert set(status.story_ids) == expected_ids  # story_ids の並び順は仕様不明
    assert len(status.story_ids) == len(expected_ids)
    sprint = env.repo.query(Sprint)[0]
    assert {s.id for s in env.repo.query(Story, sprint_id=sprint.id)} == expected_ids
    for left in {"s1", "s2", "s3"} - expected:
        assert env.repo.get(Story, ids[left]).status == StoryStatus.BACKLOG


def test_boundary_removing_last_story_leaves_empty_sprint(env: Env) -> None:
    """BV10: ストーリーが 1 件だけのスプリントから外すと、返るスプリントの stories は空で、
    planning のままであること。"""
    story_id = put_sprint_in(env, "planning")
    assert story_id is not None

    result = env.tools.remove_story_from_sprint(P, story_id)

    assert (result.stories, result.status) == ([], "planning")


@pytest.mark.parametrize(
    "titles",
    [
        pytest.param([], id="BV11-tasks-0"),
        pytest.param(["一覧 API"], id="BV12-tasks-1"),
        pytest.param(["一覧 API", "画面", "テスト"], id="BV13-tasks-many-keep-order"),
    ],
)
def test_boundary_sprint_result_lists_tasks_in_added_order(env: Env, titles: list[str]) -> None:
    """BV11–BV13: スプリントの内容のタスクは、0 件なら空、1 件以上なら追加した順のまま返ること。

    MCP のツールからは、追加した順と保存の順が常に一致する。そのため、このテストは「追加した順で返る」という
    振る舞いだけを保証する。内部の並べ替え（Task.order）が保存の順と食い違う状況は作れない。"""
    story = env.tools.add_story(P, "対象")
    env.tools.create_sprint(P, "ゴール")
    for title in titles:
        env.tools.add_task(story.id, title)

    result = env.tools.add_story_to_sprint(P, story.id)

    assert result.stories[0].tasks == titles


@pytest.mark.parametrize(
    ("open_count", "answered"),
    [
        pytest.param(0, False, id="BV14-open-spikes-0"),
        pytest.param(1, False, id="BV15-open-spikes-1"),
        pytest.param(3, False, id="BV16-open-spikes-many"),
        pytest.param(1, True, id="BV17-open-spikes-1-plus-answered"),
    ],
)
def test_boundary_sprint_result_lists_open_spikes(env: Env, open_count: int, answered: bool) -> None:
    """BV14–BV17: スプリントの内容の open_spikes は、未解決のスパイクが 0 件なら空、1 件以上ならその件数だけ
    （回答済みのスパイクは含めずに）返ること。"""
    story = env.tools.add_story(P, "対象")
    env.tools.create_sprint(P, "ゴール")
    questions = [f"質問 {i + 1}" for i in range(open_count)]
    spikes = [env.tools.add_spike(story.id, q) for q in questions]
    if answered:
        done = env.tools.add_spike(story.id, "回答済みの質問")
        env.planning.answer_spike(done.id, "回答")

    result = env.tools.add_story_to_sprint(P, story.id)

    # 並び順は仕様にないため、集合と件数で比べる
    listed = result.stories[0].open_spikes
    assert len(listed) == open_count
    assert set(listed) == {f"{sp.id}: {sp.question}" for sp in spikes}


# =====================================================================
# 例外テスト（設計表: docs/test-design/20261123-caucus-sprint-1.md の「## 例外テスト」）
# =====================================================================


def _x_approved_story(env: Env) -> str:
    story_id = put_sprint_in(env, "approved")
    assert story_id is not None
    return story_id


def _x1(env: Env, git_repo: Path) -> Callable[[], object]:
    _x_approved_story(env)
    other = env.tools.add_story(P, "追加したい").id
    return lambda: env.tools.add_story_to_sprint(P, other)


def _x2(env: Env, git_repo: Path) -> Callable[[], object]:
    story_id = _x_approved_story(env)
    return lambda: env.tools.remove_story_from_sprint(P, story_id)


def _x3(env: Env, git_repo: Path) -> Callable[[], object]:
    story_id = _x_approved_story(env)
    return lambda: env.tools.add_task(story_id, "タスク")


def _x4(env: Env, git_repo: Path) -> Callable[[], object]:
    story_id = _x_approved_story(env)
    return lambda: env.tools.add_spike(story_id, "質問")


def _x5(env: Env, git_repo: Path) -> Callable[[], object]:
    put_sprint_in(env, "planning")
    env.tools.add_story_to_sprint(P, env.tools.add_story(P, "2 件目").id)
    third = env.tools.add_story(P, "3 件目").id
    return lambda: env.tools.add_story_to_sprint(P, third)


def _x6(env: Env, git_repo: Path) -> Callable[[], object]:
    put_sprint_in(env, "planning")
    register_other(env, git_repo)
    other = env.tools.add_story(OTHER, "別プロジェクト").id
    return lambda: env.tools.add_story_to_sprint(P, other)


def _x7(env: Env, git_repo: Path) -> Callable[[], object]:
    story_id = put_sprint_in(env, "planning")
    assert story_id is not None
    return lambda: env.tools.add_story_to_sprint(P, story_id)


def _x8(env: Env, git_repo: Path) -> Callable[[], object]:
    put_sprint_in(env, "planning")
    backlog = env.tools.add_story(P, "バックログ").id
    return lambda: env.tools.remove_story_from_sprint(P, backlog)


def _x9(env: Env, git_repo: Path) -> Callable[[], object]:
    put_sprint_in(env, "planning")
    return lambda: env.tools.create_sprint(P, "2 つ目")


def _x10_add(env: Env, git_repo: Path) -> Callable[[], object]:
    story_id = env.tools.add_story(P, "対象").id
    return lambda: env.tools.add_story_to_sprint(P, story_id)


def _x10_remove(env: Env, git_repo: Path) -> Callable[[], object]:
    story_id = env.tools.add_story(P, "対象").id
    return lambda: env.tools.remove_story_from_sprint(P, story_id)


@pytest.mark.parametrize(
    ("setup", "error", "message"),
    [
        pytest.param(
            _x1, InvalidTransitionError, "approved のため計画を変更できません", id="X1-add-after-approval"
        ),
        pytest.param(_x2, InvalidTransitionError, "計画を変更できません", id="X2-remove-after-approval"),
        pytest.param(
            _x3, InvalidTransitionError, "計画中ではないため変更できません", id="X3-task-after-approval"
        ),
        pytest.param(
            _x4, InvalidTransitionError, "計画中ではないため変更できません", id="X4-spike-after-approval"
        ),
        pytest.param(_x5, DomainError, r"上限（2）に達しています", id="X5-over-limit"),
        pytest.param(_x6, DomainError, "は demo のストーリーではありません", id="X6-other-project-story"),
        pytest.param(_x7, InvalidTransitionError, "バックログにありません", id="X7-already-in-sprint"),
        pytest.param(_x8, DomainError, "スプリント #1 に含まれていません", id="X8-not-in-sprint"),
        pytest.param(
            _x9, DomainError, r"スプリント #1（planning）が終了していません", id="X9-open-sprint-exists"
        ),
        pytest.param(_x10_add, DomainError, "進行中のスプリントがありません", id="X10-add-without-sprint"),
        pytest.param(
            _x10_remove, DomainError, "進行中のスプリントがありません", id="X10-remove-without-sprint"
        ),
    ],
)
def test_exception_rule_violation_is_reported_and_changes_nothing(
    env: Env, git_repo: Path, setup: Callable, error: type[Exception], message: str
) -> None:
    """X1–X10: ルール違反のとき、理由が分かるメッセージ付きの決められた例外になり、
    ストーリー・スプリント・タスク・スパイクのどれも変わらないこと。"""
    operation = setup(env, git_repo)
    before = (
        story_snapshot(env),
        len(env.repo.query(Sprint)),
        len(env.repo.query(Task)),
        len(env.repo.query(Spike)),
    )

    with pytest.raises(error, match=message):
        operation()

    after = (
        story_snapshot(env),
        len(env.repo.query(Sprint)),
        len(env.repo.query(Task)),
        len(env.repo.query(Spike)),
    )
    assert after == before


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda t: t.add_story("nope", "x"), id="add_story"),
        pytest.param(lambda t: t.create_sprint("nope", "x"), id="create_sprint"),
        pytest.param(lambda t: t.add_story_to_sprint("nope", "ST-1"), id="add_story_to_sprint"),
        pytest.param(lambda t: t.remove_story_from_sprint("nope", "ST-1"), id="remove_story_from_sprint"),
        pytest.param(lambda t: t.plan_sprint("nope"), id="plan_sprint"),
        pytest.param(lambda t: t.get_plan_status("nope"), id="get_plan_status"),
    ],
)
def test_exception_missing_project_is_not_found(env: Env, call: Callable[[PlanningTools], object]) -> None:
    """X11, X19: 存在しないプロジェクトを指定したとき、そのプロジェクト名を含む NotFoundError になり、
    ストーリーもスプリントも増えないこと。"""
    put_sprint_in(env, "planning")  # ST-1 を planning のスプリントに入れておく
    before = (story_snapshot(env), len(env.repo.query(Sprint)))

    with pytest.raises(NotFoundError, match="プロジェクト nope が見つかりません"):
        call(env.tools)

    assert (story_snapshot(env), len(env.repo.query(Sprint))) == before
    assert env.jobs.status("nope") is None


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda t: t.add_story_to_sprint(P, "ST-999"), id="add_story_to_sprint"),
        pytest.param(lambda t: t.remove_story_from_sprint(P, "ST-999"), id="remove_story_from_sprint"),
        pytest.param(lambda t: t.add_task("ST-999", "x"), id="add_task"),
        pytest.param(lambda t: t.add_spike("ST-999", "x"), id="add_spike"),
    ],
)
def test_exception_missing_story_is_not_found(env: Env, call: Callable[[PlanningTools], object]) -> None:
    """X12: 存在しないストーリー ID を指定したとき、その ID を含む NotFoundError になり、
    何も変わらないこと。"""
    put_sprint_in(env, "planning")
    before = (story_snapshot(env), len(env.repo.query(Task)), len(env.repo.query(Spike)))

    with pytest.raises(NotFoundError, match="ST-999 が見つかりません"):
        call(env.tools)

    assert (story_snapshot(env), len(env.repo.query(Task)), len(env.repo.query(Spike))) == before


def test_exception_plan_sprint_without_ai_planner_leaves_no_job_or_lock(env: Env, git_repo: Path) -> None:
    """X14: AI プランナー未設定で plan_sprint を呼ぶと、設定方法を案内するエラーになり、
    ジョブもロックも残らず、設定を変えた後で計画を始められること。"""
    env.projects.register("manual", git_repo)

    with pytest.raises(DomainError, match="AI プランナーが設定されていません.*--planner claude"):
        env.tools.plan_sprint("manual")

    assert env.tools.get_plan_status("manual").status == "not_started"
    assert not env.jobs.running("manual")
    env.projects.configure("manual", planner_config={"type": "claude"})
    env.launcher.hold = True
    assert env.tools.plan_sprint("manual").status == "running"


def test_exception_plan_sprint_after_approval_starts_no_job(env: Env) -> None:
    """X15: 承認済みのスプリントがあるとき、plan_sprint は「終了していません」のエラーになり、
    ジョブは始まらないこと。"""
    put_sprint_in(env, "approved")

    with pytest.raises(DomainError, match=r"スプリント #1（approved）が終了していません"):
        env.tools.plan_sprint(P)

    assert env.tools.get_plan_status(P).status == "not_started"
    assert not env.jobs.running(P)


def test_exception_plan_sprint_twice_keeps_running_job(env: Env) -> None:
    """X16: 計画の作成中に plan_sprint をもう一度呼ぶと「既に作成中です」のエラーになり、
    作成中のジョブは running のまま変わらないこと。"""
    hold_plan_job(env)
    before = env.tools.get_plan_status(P)

    with pytest.raises(DomainError, match="demo のスプリント計画は既に作成中です"):
        env.tools.plan_sprint(P)

    assert env.tools.get_plan_status(P) == before
    assert before.status == "running"


def test_exception_launch_failure_is_reported_and_releases_lock(env: Env) -> None:
    """X17: 計画のプロセスを起動できない（OSError）とき、理由を含むエラーになり、状態は failed で、
    ロックが残らずもう一度 plan_sprint を呼べること。"""

    def cannot_launch(argv: list[str], log_path: Path, lock_fd: int) -> None:
        raise OSError("python が見つかりません")

    real_launcher = env.jobs.launcher
    env.jobs.launcher = cannot_launch  # プロセス境界（サブプロセスの起動）の失敗

    with pytest.raises(DomainError, match="計画の作成を開始できませんでした.*python が見つかりません"):
        env.tools.plan_sprint(P)

    status = env.tools.get_plan_status(P)
    assert status.status == "failed"
    assert "python が見つかりません" in (status.error or "")
    assert not env.jobs.running(P)
    env.jobs.launcher = real_launcher
    env.launcher.hold = True
    assert env.tools.plan_sprint(P).status == "running"


@pytest.mark.parametrize(
    "tool", ["create_sprint", "add_story_to_sprint", "remove_story_from_sprint", "add_task", "add_spike"]
)
def test_exception_plan_change_while_ai_is_planning_is_rejected(env: Env, git_repo: Path, tool: str) -> None:
    """X13: 計画の作成中に計画を変更するツールを呼ぶと、確認の方法（get_plan_status）を案内する
    「計画を作成中のため変更できません」のエラーになり、何も変わらず、
    作成中のジョブは running のままであること。"""
    operation = _prepare_edit(env, tool, P)
    hold_plan_job(env)
    before = (
        story_snapshot(env),
        len(env.repo.query(Sprint)),
        len(env.repo.query(Task)),
        len(env.repo.query(Spike)),
    )

    with pytest.raises(
        DomainError, match="demo は AI がスプリント計画を作成中のため変更できません.*get_plan_status"
    ):
        operation()

    after = (
        story_snapshot(env),
        len(env.repo.query(Sprint)),
        len(env.repo.query(Task)),
        len(env.repo.query(Spike)),
    )
    assert after == before
    assert env.tools.get_plan_status(P).status == "running"


def test_exception_unexpected_launch_error_propagates_without_stuck_running(env: Env) -> None:
    """X18: 起動器が OSError 以外の例外を投げたとき、その例外は包まずにそのまま伝わり、
    状態は failed でその例外を理由に持ち（起動していないプロセスの「異常終了」とは言わない）、
    もう一度 plan_sprint を呼べること。"""

    def broken(argv: list[str], log_path: Path, lock_fd: int) -> None:
        raise RuntimeError("起動器の不具合")

    real_launcher = env.jobs.launcher
    env.jobs.launcher = broken

    with pytest.raises(RuntimeError, match="起動器の不具合"):
        env.tools.plan_sprint(P)

    status = env.tools.get_plan_status(P)
    assert status.status == "failed"
    assert "起動器の不具合" in (status.error or "")
    assert "異常終了" not in (status.error or "")
    assert not env.jobs.running(P)
    env.jobs.launcher = real_launcher
    env.launcher.hold = True
    assert env.tools.plan_sprint(P).status == "running"


@pytest.mark.parametrize(
    ("error", "reason", "log_has_traceback"),
    [
        pytest.param(
            PlannerError("claude が失敗しました"), "claude が失敗しました", False, id="X20-planner-error"
        ),
        pytest.param(RuntimeError("予期しない故障"), "予期しない故障", True, id="X21-unexpected-error"),
    ],
)
def test_exception_plan_job_records_failure_instead_of_raising(
    env: Env,
    planner: GatedPlanner,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    reason: str,
    log_has_traceback: bool,
) -> None:
    """X20–X21: 計画の処理でプランナーの失敗や想定外の例外が起きたとき、例外を投げずに False を返し、
    状態は failed で理由を持ち、スプリントは作られず、ログに理由（想定外ならトレースバック）が残ること。"""
    story_id = env.tools.add_story(P, "候補").id
    planner.plans.append(
        SprintPlanDraft(goal="ゴール", stories=[PlannedStoryDraft(story_id=story_id, title="")])
    )
    planner.error = error
    planner.gate.set()

    with caplog.at_level(logging.ERROR, logger="caucus_race.plan_job"):
        assert run_plan_job(env.ai, env.jobs, P) is False

    status = env.tools.get_plan_status(P)
    assert status.status == "failed"
    assert reason in (status.error or "")
    assert env.repo.query(Sprint) == []
    assert env.repo.get(Story, story_id).status == StoryStatus.BACKLOG
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "失敗がログに残っていません"
    if log_has_traceback:
        assert any(r.exc_info and reason in str(r.exc_info[1]) for r in errors)
    else:
        assert any(reason in r.getMessage() for r in errors)


def test_exception_keyboard_interrupt_is_not_swallowed(env: Env, planner: GatedPlanner) -> None:
    """X22: 計画の処理で KeyboardInterrupt が起きたとき、握りつぶさずにそのまま伝わり、
    失敗や成功の結果を書かないこと（プロセスが生きている間は running のまま）。

    プロセスが終わってロックが外れた後に failed（異常終了）と判定されることは、
    組み合わせテスト G7 と状態遷移テストの R-e4 が保証する。"""
    hold_plan_job(env)  # 子プロセスとしてロックを持っている状態
    planner.error = KeyboardInterrupt()  # type: ignore[assignment]
    planner.gate.set()

    with pytest.raises(KeyboardInterrupt):
        run_plan_job(env.ai, env.jobs, P)

    status = env.tools.get_plan_status(P)
    assert (status.status, status.error, status.finished_at) == ("running", None, None)


@pytest.mark.parametrize(
    ("register", "project", "reason"),
    [
        pytest.param(True, "demo", "AI プランナーが設定されていません", id="X23-planner-not-configured"),
        pytest.param(False, "nope", "プロジェクト nope が見つかりません", id="X24-missing-project"),
    ],
)
def test_exception_cli_plan_run_exits_with_1_on_failure(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch, register: bool, project: str, reason: str
) -> None:
    """X23–X24: `caucus plan run` で計画が失敗したとき、終了コード 1 で終わり、
    失敗の理由が状態に書かれること。"""
    from typer.testing import CliRunner

    from caucus_race.cli import main as cli

    home = tmp_path / "home"
    monkeypatch.setenv("CAUCUS_HOME", str(home))
    cli.container.cache_clear()
    try:
        runner = CliRunner()
        if register:
            added = runner.invoke(cli.app, ["project", "add", project, "--repo", str(git_repo)])
            assert added.exit_code == 0

        result = runner.invoke(cli.app, ["plan", "run", project])

        assert result.exit_code == 1
        settings = Settings(data_dir=home)
        state = PlanJobs(settings.run_dir, settings.log_dir).status(project)
        assert state is not None and state.status == "failed"
        assert reason in (state.error or "")
    finally:
        cli.container.cache_clear()  # 失敗しても、この CAUCUS_HOME の container を後続に残さない


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("team/app", id="X25-slash"),
        pytest.param("../escape", id="X25-parent-dir"),
        pytest.param("..", id="X25-dot-dot"),
        pytest.param(LONG_NAME, id="X25-long-name"),
    ],
)
def test_exception_project_name_with_path_characters_stays_in_data_dirs(
    env: Env, git_repo: Path, name: str
) -> None:
    """X25: プロジェクト名に `/` や `..` が含まれていても、計画を始められ（生の OSError にならず）、
    計画ジョブのファイルは CAUCUS_HOME の run・logs の中にだけ作られること。"""
    data_dir = env.jobs.run_dir.parent
    env.projects.register(name, git_repo, planner_config={"type": "claude"})
    before = {p for p in data_dir.parent.rglob("*")}

    env.launcher.hold = True
    assert env.tools.plan_sprint(name).status == "running"
    assert env.tools.get_plan_status(name).status == "running"
    env.launcher.release()
    run_plan_job(env.ai, env.jobs, name)  # 子プロセスの代わりに結果を書く（計画がないので失敗する）

    assert env.tools.get_plan_status(name).status == "failed"
    created = {p for p in data_dir.parent.rglob("*") if p.is_file()} - before
    assert created, "計画ジョブのファイルが作られていません"
    for path in created:
        assert path.parent in (env.jobs.run_dir, env.jobs.log_dir), f"想定外の場所に作られました: {path}"
    assert env.tools.get_plan_status(P).status == "not_started"  # 他のプロジェクトの状態と混ざらない


def test_exception_unreadable_state_file_does_not_hide_saved_plan(
    env: Env, planner: GatedPlanner, caplog: pytest.LogCaptureFixture
) -> None:
    """X26: 計画を DB に保存したあと、状態ファイルが壊れていて読めなくても、結果（succeeded）を書き直し、
    get_plan_status が実際の計画（スプリント番号・ストーリー）を返すこと。"""
    story_id = env.tools.add_story(P, "候補").id
    planner.plans.append(
        SprintPlanDraft(goal="ゴール", stories=[PlannedStoryDraft(story_id=story_id, title="")])
    )
    planner.gate.set()
    hold_plan_job(env)
    state_files = list(env.jobs.run_dir.glob("*.plan.json"))
    assert state_files, "状態ファイルが見つかりません（ファイル名の規則が変わった）"
    for state_file in state_files:  # ファイル境界: 状態ファイルを壊す
        state_file.write_text("{壊れた JSON", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="caucus_race.plan_job"):
        assert run_plan_job(env.ai, env.jobs, P) is True

    status = env.tools.get_plan_status(P)
    assert (status.status, status.sprint_number, status.story_ids) == ("succeeded", 1, [story_id])
    assert any("状態ファイルが読めない" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        pytest.param(_x1, "approved のため計画を変更できません", id="X27-invalid-transition"),
        pytest.param(_x5, r"上限（2）に達しています", id="X27-domain-error"),
        pytest.param(
            lambda env, git_repo: lambda: env.tools.add_task("ST-999", "x"),
            "ST-999 が見つかりません",
            id="X27-not-found",
        ),
    ],
)
def test_exception_call_tool_turns_rule_violation_into_tool_failure(
    env: Env, git_repo: Path, setup: Callable, message: str
) -> None:
    """X27: MCP のツールとして呼んだとき、ルール違反（DomainError のサブクラスを含む）は例外ではなく、
    CLI と同じメッセージを持つ失敗の結果（ToolFailure）になり、何も変わらないこと。"""
    operation = setup(env, git_repo)
    before = story_snapshot(env)

    result = call_tool(operation)

    assert isinstance(result, ToolFailure)
    assert re.search(message, result.message)
    assert story_snapshot(env) == before


def test_exception_call_tool_passes_through_unexpected_errors(env: Env) -> None:
    """X28: MCP のツールとして呼んだとき、想定外の例外（ルール違反ではないもの）は失敗の結果に変えず、
    そのまま伝わること（SDK が汎用のエラーにし、トレースバックをログに出す）。"""

    def broken(argv: list[str], log_path: Path, lock_fd: int) -> None:
        raise RuntimeError("起動器の不具合")

    env.jobs.launcher = broken
    with pytest.raises(RuntimeError, match="起動器の不具合"):
        call_tool(lambda: env.tools.plan_sprint(P))


def test_exception_call_tool_returns_value_on_success(env: Env) -> None:
    """X29: MCP のツールとして呼んで成功したとき、ツールの戻り値がそのまま返ること。"""
    result = call_tool(lambda: env.tools.add_story(P, "一覧画面"))
    assert not isinstance(result, ToolFailure)
    assert env.repo.get(Story, result.id).title == "一覧画面"


# =====================================================================
# 状態遷移テスト（設計表: docs/test-design/20261123-caucus-sprint-1.md の「## 状態遷移テスト」）
# =====================================================================


class JobDriver:
    """計画ジョブにイベントを与え、状態（N / R / S / F / D / C）を観察する。

    ジョブのプロセス（プロセス境界）は、ロックを持つ記述子（hold）と、
    テストの中で呼ぶ run_plan_job で代用する。
    プロセスの終了は、持っているロックを手放すこと（release）で表す。
    """

    def __init__(self, env: Env, planner: GatedPlanner) -> None:
        self.env = env
        self.planner = planner
        planner.gate.set()
        self.count = 0
        # e7（計画の変更）の対象。事前に用意しておき、e7 ではタスクの追加だけを行う
        self.target = env.tools.add_story(P, "変更の対象").id

    # --- 観察 ---

    def status(self) -> PlanJobResult:
        return self.env.tools.get_plan_status(P)

    def observe(self) -> str:
        status = self.status()
        if status.status == "failed" and "異常終了" in (status.error or ""):
            return "D"
        return {"not_started": "N", "running": "R", "succeeded": "S", "failed": "F"}[status.status]

    def describe(self) -> str:
        """落ちたときに原因が分かるよう、問い合わせの結果をそのまま返す。"""
        try:
            return self.status().model_dump_json()
        except Exception as e:  # 状態ファイルが壊れている（C）など
            return f"{type(e).__name__}: {e}"

    def state_files(self) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in self.env.jobs.run_dir.glob("*.plan.json")}

    # --- イベント ---

    def e1_start(self) -> object:
        self.env.launcher.hold = True
        try:
            return self.env.tools.plan_sprint(P)
        finally:
            self.env.launcher.hold = False

    def _job_process_finishes(self, *, success: bool) -> None:
        """作成中（R）のジョブのプロセスが計画を作り、結果を書いて終わる。"""
        self.count += 1
        if success:
            story = self.env.tools.add_story(P, f"計画対象 {self.count}")
            self.planner.plans.append(
                SprintPlanDraft(
                    goal=f"ゴール {self.count}", stories=[PlannedStoryDraft(story_id=story.id, title="")]
                )
            )
        else:
            self.planner.error = PlannerError(f"プランナーの失敗 {self.count}")
        try:
            run_plan_job(self.env.ai, self.env.jobs, P)
        finally:
            self.planner.error = None
            self.env.launcher.release()  # プロセスの終了でロックが外れる

    def e2_success(self) -> None:
        self._job_process_finishes(success=True)

    def e3_failure(self) -> None:
        self._job_process_finishes(success=False)

    def e4_die(self) -> None:
        self.env.launcher.release()

    def _launch_with(self, error: Exception) -> object:
        def cannot_launch(argv: list[str], log_path: Path, lock_fd: int) -> None:
            raise error

        real = self.env.jobs.launcher
        self.env.jobs.launcher = cannot_launch  # プロセス境界（サブプロセスの起動）の失敗
        try:
            return self.env.tools.plan_sprint(P)
        finally:
            self.env.jobs.launcher = real

    def e5_launch_oserror(self) -> object:
        return self._launch_with(OSError("起動できません"))

    def e5b_launch_other_error(self) -> object:
        return self._launch_with(RuntimeError("起動器の不具合"))

    def e6_query(self) -> object:
        return self.env.tools.get_plan_status(P)

    def e7_change_plan(self) -> object:
        return self.env.tools.add_task(self.target, "タスク")

    def prepare_e8(self) -> None:
        """e8 の前提: スプリントを承認済みにする（人間が CLI で承認した状態）。

        ジョブの作成中でも人間は CLI で承認できるため、ユースケースを直接使う（MCP のロックを通らない）。
        """
        open_ = [s for s in self.env.repo.query(Sprint) if s.status == SprintStatus.PLANNING]
        if not open_:
            self.env.planning.new_sprint(P, "人間が作ったスプリント")
            self.env.planning.add_story(P, self.env.tools.add_story(P, "人間が選んだ").id)
        self.env.planning.approve(P)

    def e8_start_not_plannable(self) -> object:
        return self.env.tools.plan_sprint(P)

    def enter(self, state: str) -> None:
        """初期状態 N から、指定した状態まで遷移させる。"""
        if state == "N":
            return
        self.e1_start()
        if state == "R":
            return
        if state in ("S", "C"):
            self.e2_success()
        elif state == "F":
            self.e3_failure()
        elif state == "D":
            self.e4_die()
        if state == "C":  # ファイル境界: プロセスのない状態で、状態ファイルを壊す
            paths = list(self.env.jobs.run_dir.glob("*.plan.json"))
            assert paths, "状態ファイルが見つかりません（ファイル名の規則が変わった）"
            for path in paths:
                path.write_text("{壊れた JSON", encoding="utf-8")
            return
        assert self.observe() == state, f"事前状態 {state} を作れませんでした: {self.describe()}"


RAISES = "raises"
SAME = "same"  # 遷移しない（無効な遷移・問い合わせ）。ジョブの状態も計画も変わらないことを確かめる
# (状態, イベント, 期待)。期待は遷移先の状態（SAME を含む）、または (RAISES, 例外の型, メッセージ, 遷移先)。
TRANSITIONS: list[tuple[str, str, object]] = []
for _state in ("N", "S", "F", "D", "C"):
    TRANSITIONS += [
        (_state, "e1", "R"),
        (_state, "e5", (RAISES, DomainError, "計画の作成を開始できませんでした.*起動できません", "F")),
        (_state, "e5b", (RAISES, RuntimeError, "起動器の不具合", "F")),
        (_state, "e7", _state),
        (_state, "e8", (RAISES, DomainError, "終了していません", SAME)),
    ]
    if _state != "C":  # C の問い合わせは仕様不明
        TRANSITIONS.append((_state, "e6", SAME))
TRANSITIONS += [
    ("R", "e1", (RAISES, DomainError, "既に作成中です", SAME)),
    ("R", "e2", "S"),
    ("R", "e3", "F"),
    ("R", "e4", "D"),
    ("R", "e5", (RAISES, DomainError, "既に作成中です", SAME)),
    ("R", "e5b", (RAISES, DomainError, "既に作成中です", SAME)),
    ("R", "e6", SAME),
    ("R", "e7", (RAISES, DomainError, "計画を作成中のため変更できません", SAME)),
    # どちらの理由で拒否するかは仕様不明。拒否されて何も変わらないことだけを確かめる
    ("R", "e8", (RAISES, DomainError, "終了していません|既に作成中です", SAME)),
]


@pytest.mark.parametrize(
    ("state", "event", "expected"),
    [pytest.param(s, e, x, id=f"{s}-{e}") for s, e, x in TRANSITIONS],
)
def test_state_plan_job_transition(
    env: Env, planner: GatedPlanner, state: str, event: str, expected: object
) -> None:
    """状態遷移表の各セル: 計画ジョブが state のときに event が起きると、表のとおりの状態に遷移すること。

    - 有効な遷移では、遷移先の状態の中身（この回の結果・新しいジョブ）まで確かめる
    - 無効な遷移では、理由を示す例外になり、ジョブの状態も計画（ストーリー・タスク）も変わらないこと
    """
    d = JobDriver(env, planner)
    d.enter(state)
    if event == "e8":
        d.prepare_e8()
    actions = {
        "e1": d.e1_start,
        "e2": d.e2_success,
        "e3": d.e3_failure,
        "e4": d.e4_die,
        "e5": d.e5_launch_oserror,
        "e5b": d.e5b_launch_other_error,
        "e6": d.e6_query,
        "e7": d.e7_change_plan,
        "e8": d.e8_start_not_plannable,
    }
    before = d.status() if state != "C" else None
    files_before = d.state_files()
    # 状態ファイルがあるはずの状態で空なら、ファイル名の規則が変わって比較が空振りしている
    assert bool(files_before) == (state != "N"), files_before.keys()
    stories_before = story_snapshot(env)
    tasks_before = len(env.repo.query(Task))

    if isinstance(expected, tuple):
        _, error, message, target = expected
        with pytest.raises(error, match=message):
            actions[event]()
    else:
        target = expected
        actions[event]()

    if target == SAME:  # 無効な遷移・問い合わせ: ジョブの状態も計画も変わらない
        assert d.state_files() == files_before, d.describe()
        if before is not None:
            assert d.status() == before, d.describe()
        assert story_snapshot(env) == stories_before
        assert len(env.repo.query(Task)) == tasks_before
        return
    if event == "e7":  # 作成中でなければ計画を変更でき、ジョブの状態は変わらない
        assert [t.title for t in env.repo.query(Task, story_id=d.target)] == ["タスク"]
        assert d.state_files() == files_before, d.describe()
        return

    assert d.observe() == target, d.describe()
    after = d.status()
    if event == "e1":  # 新しいジョブの状態が書かれている（前の結果を引きずらない）
        assert (after.error, after.finished_at, after.sprint_number, after.story_ids) == (
            None,
            None,
            None,
            [],
        )
        if before is not None:
            assert after.started_at != before.started_at
    elif event == "e2":  # この回の計画の結果
        assert (after.goal, after.error) == (f"ゴール {d.count}", None)
    elif event == "e3":  # この回の失敗の理由
        assert f"プランナーの失敗 {d.count}" in (after.error or "")
    elif event == "e5":
        assert "起動できません" in (after.error or "")
    elif event == "e5b":
        assert "起動器の不具合" in (after.error or "")


# --- 重要な遷移の列 ---


def test_state_sequence_start_succeed_replan_fail_retry_succeed(env: Env, planner: GatedPlanner) -> None:
    """Q1: 開始 → 問い合わせ → 成功 → やり直し → 失敗 → 再試行 → 成功の各段階で、状態と結果が正しく、
    前の結果が次の結果で置き換わること。"""
    d = JobDriver(env, planner)
    assert d.observe() == "N"
    assert d.e1_start().status == "running"  # type: ignore[attr-defined]
    assert d.observe() == "R"
    assert d.observe() == "R"  # 問い合わせでは変わらない
    d.e2_success()
    first = d.status()
    assert (first.status, first.sprint_number, first.goal) == ("succeeded", 1, "ゴール 1")
    d.e1_start()
    assert d.observe() == "R"
    d.e3_failure()
    failed = d.status()
    assert failed.status == "failed"
    assert "プランナーの失敗 2" in (failed.error or "")
    assert failed.goal is None  # 前の成功の結果を引きずらない
    d.e1_start()
    assert d.observe() == "R"
    d.e2_success()
    last = d.status()
    assert (last.status, last.sprint_number, last.goal, last.error) == ("succeeded", 1, "ゴール 3", None)


def test_state_sequence_die_then_retry_succeeds(env: Env, planner: GatedPlanner) -> None:
    """Q2: 異常終了の後に再試行でき、成功すると異常終了の判定が成功の結果で置き換わること。"""
    d = JobDriver(env, planner)
    d.e1_start()
    d.e4_die()
    assert d.observe() == "D"
    d.e1_start()
    assert d.observe() == "R"
    d.e2_success()
    assert (d.observe(), d.status().error) == ("S", None)


def test_state_sequence_double_start_keeps_original_job(env: Env, planner: GatedPlanner) -> None:
    """Q3: 作成中の二重実行は拒否され、元のジョブは壊れずに成功し、その後は再び開始できること。"""
    d = JobDriver(env, planner)
    d.e1_start()
    started = d.status()
    with pytest.raises(DomainError, match="既に作成中です"):
        d.e1_start()
    assert d.status() == started
    d.e2_success()
    assert d.observe() == "S"
    d.e1_start()
    assert d.observe() == "R"


def test_state_sequence_repeated_launch_failures_then_start(env: Env, planner: GatedPlanner) -> None:
    """Q4: 起動の失敗が続いてもロックが残らず、最後に開始して成功できること。"""
    d = JobDriver(env, planner)
    for _ in range(2):
        with pytest.raises(DomainError, match="計画の作成を開始できませんでした"):
            d.e5_launch_oserror()
        assert d.observe() == "F"
    d.e1_start()
    assert d.observe() == "R"
    d.e2_success()
    assert d.observe() == "S"


def test_state_sequence_plan_change_blocked_only_while_running(env: Env, planner: GatedPlanner) -> None:
    """Q5: 作成中は計画の変更が拒否されて何も変わらず、作成が終わった後は同じ変更ができること。"""
    d = JobDriver(env, planner)
    d.e1_start()
    with pytest.raises(DomainError, match="計画を作成中のため変更できません"):
        d.e7_change_plan()
    assert env.repo.query(Task, story_id=d.target) == []
    assert d.observe() == "R"
    d.e2_success()
    assert d.observe() == "S"
    d.e7_change_plan()
    assert [t.title for t in env.repo.query(Task, story_id=d.target)] == ["タスク"]


def test_state_sequence_approved_while_running_then_job_fails_and_replan_is_rejected(
    env: Env, planner: GatedPlanner
) -> None:
    """Q6: 作成中に人間が CLI でスプリントを承認すると、ジョブは「終了していません」で失敗（F）し、
    承認済みの計画は AI に置き換えられず、その後の plan_sprint も拒否されて F のままであること。"""
    d = JobDriver(env, planner)
    d.e1_start()
    d.prepare_e8()  # 作成中に人間が承認する
    approved = story_snapshot(env)
    d.e2_success()  # ジョブのプロセスが計画を反映しようとして失敗する
    failed = d.status()
    assert failed.status == "failed"
    assert "終了していません" in (failed.error or "")
    assert [s for s in story_snapshot(env) if s[0] in {a[0] for a in approved}] == approved
    with pytest.raises(DomainError, match="終了していません"):
        d.e8_start_not_plannable()
    assert d.status() == failed


def test_state_sequence_concurrent_double_start_allows_only_one(env: Env, planner: GatedPlanner) -> None:
    """Q7: 2 つの開始が同時に来ても、始まるジョブは 1 つだけで、
    もう 1 つは「既に作成中です」で拒否されること。

    二重実行を防ぐのはロック（PlanJobs.start）なので、同時に start を呼んで確かめる（10 回繰り返す）。
    """
    env.launcher.hold = True
    for _ in range(10):
        barrier = threading.Barrier(2)
        outcomes: list[object] = []

        def start(barrier: threading.Barrier = barrier, outcomes: list[object] = outcomes) -> None:
            barrier.wait()
            try:
                outcomes.append(env.jobs.start(P))
            except DomainError as e:
                outcomes.append(e)

        threads = [threading.Thread(target=start) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        started = [o for o in outcomes if not isinstance(o, Exception)]
        rejected = [o for o in outcomes if isinstance(o, DomainError)]
        assert (len(started), len(rejected)) == (1, 1), outcomes
        assert "既に作成中です" in str(rejected[0])
        assert len(env.launcher.held) == 1  # 起動されたプロセスも 1 つだけ
        env.launcher.release()


# =====================================================================
# 再現テスト（レビュー指摘）。修正前は落ち、修正後に通る
# =====================================================================


def test_repro_plan_does_not_override_sprint_approved_while_planner_runs(
    env: Env, planner: GatedPlanner
) -> None:
    """レビュー指摘（ai_planning.py の plan が状態を確かめ直さない）:
    AI の計画の作成中（プランナーの呼び出し中）に人間が CLI でスプリントを承認したとき、
    完了したジョブは承認済みの計画を AI の計画で置き換えず、失敗として報告すること（R-3）。"""
    put_sprint_in(env, "planning")
    candidate = env.tools.add_story(P, "AI が選ぶ").id
    planner.plans.append(
        SprintPlanDraft(goal="AI のゴール", stories=[PlannedStoryDraft(story_id=candidate, title="")])
    )
    env.tools.plan_sprint(P)
    assert planner.called.wait(5)
    env.planning.approve(P)  # プランナーの呼び出し中に、人間が承認する
    approved_stories = story_snapshot(env)

    planner.gate.set()
    env.launcher.join()

    sprint = env.repo.query(Sprint)[0]
    assert (sprint.status, sprint.goal) == (SprintStatus.APPROVED, "既存のゴール")
    assert story_snapshot(env) == approved_stories
    assert env.tools.get_plan_status(P).status == "failed"


def test_repro_plan_does_not_create_second_open_sprint_when_human_creates_one_meanwhile(
    env: Env, planner: GatedPlanner
) -> None:
    """レビュー指摘（同上）: スプリントがない状態で計画を始め、
    プランナーの呼び出し中に人間がスプリントを作ったとき、
    完了したジョブは 2 つ目の未終了のスプリントを作らず、失敗として報告すること。"""
    candidate = env.tools.add_story(P, "AI が選ぶ").id
    planner.plans.append(
        SprintPlanDraft(goal="AI のゴール", stories=[PlannedStoryDraft(story_id=candidate, title="")])
    )
    env.tools.plan_sprint(P)
    assert planner.called.wait(5)
    env.planning.new_sprint(P, "人間のスプリント")  # プランナーの呼び出し中に、人間が作る

    planner.gate.set()
    env.launcher.join()

    open_sprints = [s for s in env.repo.query(Sprint) if s.status != SprintStatus.CLOSED]
    assert [s.goal for s in open_sprints] == ["人間のスプリント"]
    assert env.repo.get(Story, candidate).status == StoryStatus.BACKLOG
    assert env.tools.get_plan_status(P).status == "failed"


def test_repro_status_reports_success_when_job_finishes_between_read_and_lock_check(
    env: Env, planner: GatedPlanner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """レビュー指摘（plan_job.py の status の競合）: 問い合わせが状態ファイルを読んだ直後に、
    ジョブのプロセスが成功の結果を書いて終わったとき、
    問い合わせは「途中で異常終了」ではなく succeeded を返すこと。

    競合の瞬間を作るため、ロックを確かめる OS の呼び出し（fcntl.flock、プロセス境界）の直前に、
    ジョブのプロセスが結果を書いて終わる処理を差し込む。"""
    story_id = env.tools.add_story(P, "計画対象").id
    planner.plans.append(
        SprintPlanDraft(goal="ゴール", stories=[PlannedStoryDraft(story_id=story_id, title="")])
    )
    planner.gate.set()
    hold_plan_job(env)  # ジョブのプロセスがロックを持って作成中
    real_flock = fcntl.flock
    finished: list[bool] = []

    def flock_after_job_finishes(fd: int, operation: int) -> None:
        if not finished and operation == fcntl.LOCK_SH | fcntl.LOCK_NB:
            finished.append(True)
            run_plan_job(env.ai, env.jobs, P)  # ジョブのプロセスが成功の結果を書き、
            env.launcher.release()  # 終了してロックが外れる
        real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", flock_after_job_finishes)
    status = env.tools.get_plan_status(P)
    monkeypatch.undo()

    assert finished, "競合の瞬間を作れませんでした"
    assert status.status == "succeeded", status.error
    assert status.story_ids == [story_id]


def test_repro_detached_plan_process_accepts_project_name_starting_with_dash(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """レビュー指摘（plan_runner_argv）: `-` で始まるプロジェクト名でも、別プロセスの計画の処理がその名前を
    プロジェクト名として受け取り（オプションと解釈されず）、そのプロジェクトの結果を書くこと。

    本物の AI プランナーは呼べないため、AI プランナー未設定のプロジェクトで
    「そのプロジェクトのプランナーのエラー」が結果に書かれることで確かめる。"""
    name = "-x"
    home = tmp_path / "home"
    monkeypatch.setenv("CAUCUS_HOME", str(home))
    settings = Settings(data_dir=home)
    Injector([AppModule(settings)]).get(ProjectService).register(name, git_repo)
    jobs = PlanJobs(settings.run_dir, settings.log_dir, launch_detached)

    jobs.start(name)

    deadline = time.monotonic() + 30
    while (state := jobs.status(name)) is not None and state.status == "running":
        assert time.monotonic() < deadline, "計画のプロセスが終わりませんでした"
        time.sleep(0.2)
    assert state is not None and state.status == "failed"
    assert "AI プランナーが設定されていません" in (state.error or ""), state.error


def test_repro_projects_differing_only_in_case_have_separate_plan_jobs(env: Env, git_repo: Path) -> None:
    """レビュー指摘（file_stem と大文字小文字を区別しないファイルシステム）: 大文字小文字だけが違う 2 つの
    プロジェクトは、計画ジョブ（ロックと状態）を共有せず、一方の作成中にもう一方の計画を始められること。"""
    env.projects.register("Foo", git_repo, planner_config={"type": "claude"})
    env.projects.register("foo", git_repo, planner_config={"type": "claude"})
    hold_plan_job(env, "Foo")

    assert env.tools.get_plan_status("foo").status == "not_started"
    env.launcher.hold = True
    assert env.tools.plan_sprint("foo").status == "running"
    assert env.tools.get_plan_status("Foo").status == "running"
