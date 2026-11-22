"""人間の判断にあたる操作のツール（HumanTools）と BackgroundRunner の組み合わせテスト（ST-3）。

設計表は docs/test-design/20261123-caucus-sprint-1.md の「## 組み合わせテスト」の I〜R にある。
ケースの ID は表の行番号（I1 など）と同値クラスの組を表す。

モックするのはプロセスの起動（spawn）だけ（tests/test_mcp_human_tools.py の FakeSpawn）。
例外になる組み合わせは「DomainError になり、状態が変わらず、プロセスを起動しない」までを確かめる。
例外の型・メッセージの詳細は例外テストの観点で扱う。
"""

import fcntl
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from injector import Binder, Injector, InstanceProvider, Module

from caucus_race.adapter.executor.fake import write_note
from caucus_race.adapter.notifier.console import RecordingNotifier
from caucus_race.container import AppModule
from caucus_race.domain.entities import ExecutionResult, Feedback, Spike, Story
from caucus_race.domain.errors import DomainError
from caucus_race.domain.status import SpikeStatus, SprintStatus, StorySource, StoryStatus
from caucus_race.mcp.human_tools import HumanTools
from caucus_race.orchestrator import Orchestrator
from caucus_race.port.executor import Brief
from caucus_race.port.planner import FeedbackConversionDraft, FeedbackStoryDraft, ReviewGuideDraft
from caucus_race.runner import BackgroundRunner, RunLock, refine_argv, runner_argv
from caucus_race.usecase.common import latest_sprint, project_by_name
from caucus_race.usecase.project import ProjectService
from caucus_race.usecase.review import ReviewService
from caucus_race.usecase.sprint import SprintService
from tests.conftest import FakeAdaptersModule
from tests.test_mcp_human_tools import MANUAL, Env, FakeSpawn, P

STATES = ["none", "planning", "approved", "active", "review", "closed"]
EXPLICIT_MANUAL = "explicit-manual"
# planner_config に type を含まない（model だけを設定した）プロジェクト。register は空の設定にだけ
# {"type": "manual"} を補うため、type を省略した状態を作るには type 以外の項目を入れる
TYPE_OMITTED = "type-omitted"


@pytest.fixture
def env(tmp_path: Path, git_repo: Path) -> Env:
    e = Env(tmp_path, git_repo)
    projects = e.injector.get(ProjectService)
    projects.register(EXPLICIT_MANUAL, git_repo, planner_config={"type": "manual"})
    projects.register(TYPE_OMITTED, git_repo, planner_config={"model": "claude-sonnet-5-5"})
    return e


def blocked(brief: Brief, workdir: Path) -> ExecutionResult:
    return ExecutionResult(status="blocked", blocked_reason="判断が必要")


def errored(brief: Brief, workdir: Path) -> ExecutionResult:
    return ExecutionResult(status="error", error="実行器の故障")


def put_sprint_in(env: Env, state: str, project: str = P, outcome: str = "done") -> Story | None:
    """スプリントの事前状態 S を作る。スプリントに入れたストーリーを返す（none なら None）。

    outcome は実行したときのストーリーの結果（done / failed / suspended）。review 以降で効く。
    """
    if state == "none":
        return None
    story = env.backlog.add_story(project, "既存のストーリー")
    env.planning.new_sprint(project, "既存のゴール")
    env.planning.add_story(project, story.id)
    if state != "planning":
        env.planning.approve(project)
    if state not in ("planning", "approved"):
        sprint = env.injector.get(SprintService).start(project)
        if state != "active":
            env.executor.default = {"done": write_note, "failed": errored, "suspended": blocked}[outcome]
            env.injector.get(Orchestrator).run(sprint.id)
            env.executor.default = write_note
        if state == "closed":
            env.injector.get(ReviewService).close(project)
    return env.repo.get(Story, story.id)


def sprint_state(env: Env, project: str = P) -> tuple[str, str | None]:
    """直近のスプリントの（状態, 差し戻しコメント）。スプリントがなければ ("none", None)。"""
    sprint = latest_sprint(env.repo, project_by_name(env.repo, project))
    return (sprint.status, sprint.plan_comment) if sprint else ("none", None)


LOCK_STATES = ["idle", "stale", "running", "running-pid-unknown", "other-project-running"]
# 実行中とみなすべきロックの状態（二重に起動してはいけない）
RUNNING_LOCKS = ("running", "running-pid-unknown")


@contextmanager
def lock_state(env: Env, kind: str) -> Iterator[None]:
    """実行ロック R の事前状態を作る。

    - idle: ロックファイルがない
    - stale: 前回の実行が終わり、空のロックファイルだけが残る
      （RunLock はファイルを消さない。2 回目以降の普通の状態）
    - running: スプリントを実行中のプロセスの代わりに、P のロックを持つ
    - running-pid-unknown: ロックは持たれているが、中身が空で pid が読めない
    - other-project-running: 別のプロジェクト（MANUAL）のロックだけが持たれている
    """
    run_dir = env.settings.run_dir
    if kind == "idle":
        yield
    elif kind == "stale":
        with RunLock(run_dir, P).hold(Path("old.log")):
            pass
        yield
    elif kind == "running":
        with RunLock(run_dir, P).hold():
            yield
    elif kind == "running-pid-unknown":
        path = RunLock(run_dir, P).path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)
    elif kind == "other-project-running":
        with RunLock(run_dir, MANUAL).hold():
            yield
    else:
        raise AssertionError(kind)


def real_planner_tools(env: Env) -> HumanTools:
    """プランナーを偽物に差し替えない（設定どおりのプランナーを使う）HumanTools。DB は env と共有する。"""
    runner = env.injector.get(BackgroundRunner)

    class RunnerModule(Module):
        def configure(self, binder: Binder) -> None:
            binder.bind(BackgroundRunner, to=InstanceProvider(runner))

    injector = Injector(
        [AppModule(env.settings), FakeAdaptersModule(env.executor, RecordingNotifier()), RunnerModule()]
    )
    return injector.get(HumanTools)


# --- I. start_sprint(project) ---


@pytest.mark.parametrize("lock", LOCK_STATES)
@pytest.mark.parametrize("state", STATES)
def test_I1_30_start_sprint_launches_only_when_approved_and_not_running(
    env: Env, state: str, lock: str
) -> None:
    """S × R の直積（I1〜I30）: 承認済みで、P が実行中でない（ロックなし・前回のロックファイルだけ残る・
    別のプロジェクトだけ実行中）ときだけ、スプリントを開始して実行プロセスを起動すること。
    それ以外（pid が読めないロックも実行中とみなす）は例外になり、
    状態は変わらず、プロセスを起動しないこと。"""
    put_sprint_in(env, state)
    before = sprint_state(env)

    with lock_state(env, lock):
        if state == "approved" and lock not in RUNNING_LOCKS:
            result = env.tools.start_sprint(P)
            [(argv, log_path)] = env.spawn.calls
            assert argv == runner_argv(P, log_path)
            assert (result.pid, result.log_path) == (env.spawn.pid, str(log_path))
            assert sprint_state(env)[0] == SprintStatus.ACTIVE
        else:
            with pytest.raises(DomainError):
                env.tools.start_sprint(P)
            assert sprint_state(env) == before
            assert env.spawn.calls == []


def test_I31_start_sprint_with_open_spike_fails_without_launching(env: Env) -> None:
    """承認済み・実行中でなくても、未解決のスパイクが残っているとき（危険な組み合わせ）、
    例外になり、スプリントを開始せず、プロセスを起動しないこと（R-2）。"""
    story = put_sprint_in(env, "approved")
    assert story is not None
    # 承認後にスパイクを起票する手段がないため、計画中に起票してから承認し直す
    env.planning.reject(P, "スパイクを足す")
    env.planning.add_spike(story.id, "並び順は？")
    env.planning.approve(P)

    with pytest.raises(DomainError):
        env.tools.start_sprint(P)

    assert sprint_state(env)[0] == SprintStatus.APPROVED
    assert env.spawn.calls == []


def test_I32_start_sprint_of_missing_project_fails_without_launching(env: Env) -> None:
    """存在しないプロジェクトを指定したとき、例外になり、プロセスを起動しないこと。"""
    with pytest.raises(DomainError):
        env.tools.start_sprint("nope")
    assert env.spawn.calls == []


class OSErrorSpawn(FakeSpawn):
    def __call__(self, argv: list[str], log_path: Path) -> int:
        raise OSError("起動できない")


def test_I33_start_sprint_when_spawn_fails_raises_domain_error(env: Env) -> None:
    """承認済み・実行中でないが、プロセスの起動に失敗したとき（OSError）、トレースバックになる例外ではなく
    DomainError（ツールエラー）になること。開始後のスプリントの状態は仕様不明のため確かめない。"""
    put_sprint_in(env, "approved")
    env.injector.get(BackgroundRunner).spawn = OSErrorSpawn()

    with pytest.raises(DomainError):
        env.tools.start_sprint(P)


# --- J. resume_sprint(project) ---


@pytest.mark.parametrize("lock", RUNNING_LOCKS)
@pytest.mark.parametrize("state", STATES)
def test_J1_12_resume_sprint_while_running_fails_without_launching(env: Env, state: str, lock: str) -> None:
    """P が実行中（pid が読めないロックを含む）のとき、S によらず例外になり、
    プロセスを二重に起動しないこと（J1〜J12）。"""
    put_sprint_in(env, state)
    before = sprint_state(env)

    with lock_state(env, lock), pytest.raises(DomainError):
        env.tools.resume_sprint(P)

    assert sprint_state(env) == before
    assert env.spawn.calls == []


@pytest.mark.parametrize(
    "lock",
    [
        pytest.param("idle", id="J13-idle"),
        pytest.param("stale", id="J14-stale-lock-file"),
        pytest.param("other-project-running", id="J15-other-project-running"),
    ],
)
def test_J13_15_resume_sprint_of_stopped_active_sprint_launches(env: Env, lock: str) -> None:
    """active で P が実行中でない
    （ロックなし・前回のロックファイルだけ残る・別のプロジェクトだけ実行中）とき、
    実行プロセスを起動して pid とログの保存先を返すこと。"""
    put_sprint_in(env, "active")

    with lock_state(env, lock):
        result = env.tools.resume_sprint(P)

    [(argv, log_path)] = env.spawn.calls
    assert argv == runner_argv(P, log_path)
    assert (result.pid, result.log_path) == (env.spawn.pid, str(log_path))
    assert sprint_state(env)[0] == SprintStatus.ACTIVE


def test_J16_resume_sprint_of_missing_project_fails_without_launching(env: Env) -> None:
    """存在しないプロジェクトを指定したとき、例外になり、プロセスを起動しないこと。"""
    with pytest.raises(DomainError):
        env.tools.resume_sprint("nope")
    assert env.spawn.calls == []


# --- K. stop_sprint(project) ---


@pytest.mark.parametrize(
    ("state", "lock", "project"),
    [
        pytest.param("active", "idle", P, id="K1-active-no-lock-file"),
        pytest.param("none", "idle", P, id="K2-no-sprint-no-lock-file"),
        pytest.param("review", "idle", P, id="K3-review-no-lock-file"),
        pytest.param("active", "stale", P, id="K4-active-stale-lock-file"),
        pytest.param("none", "idle", "nope", id="K5-missing-project"),
        pytest.param("active", "running-pid-unknown", P, id="K6-active-running-pid-unknown"),
        pytest.param("active", "other-project-running", P, id="K7-active-other-project-running"),
    ],
)
def test_K1_7_stop_sprint_without_identifiable_process_fails(
    env: Env, state: str, lock: str, project: str
) -> None:
    """止めるプロセスを特定できないとき（実行中でない・前回のロックファイルだけ残る・プロジェクトが存在しない・
    別のプロジェクトだけ実行中・ロックは持たれているが pid が読めない）、S によらず例外になること。
    pid が読めないときに誤ったプロセス（pid 0 はこのテスト自身のプロセスグループ）へ停止を送ると、
    テストのプロセスが止まって失敗する。"""
    put_sprint_in(env, state)

    with lock_state(env, lock), pytest.raises(DomainError):
        env.tools.stop_sprint(project)


# --- L. answer_spike(spike_id, answer) ---

PLANNER_PROJECT = {"claude": P, "manual-explicit": EXPLICIT_MANUAL, "type-omitted": TYPE_OMITTED}


@pytest.mark.parametrize("remaining", ["none", "same-story", "other-story-only"])
@pytest.mark.parametrize("planner", list(PLANNER_PROJECT))
def test_L1_9_answer_spike_starts_refine_only_with_ai_planner_and_no_open_spike_in_story(
    env: Env, planner: str, remaining: str
) -> None:
    """プランナー設定 × 残りのスパイクの直積（L1〜L9）: 回答は常に決定事項に 1 件追加され、
    返り値の remaining_spikes はそのストーリーに残る未解決のスパイクだけになり、
    見直しは「AI プランナーが設定済み」かつ「そのストーリーの未解決のスパイクがない」ときだけ起動すること。
    別のストーリーの未解決のスパイクは条件にも remaining_spikes にも影響しないこと。"""
    project = PLANNER_PROJECT[planner]
    story = put_sprint_in(env, "planning", project)
    assert story is not None
    spike = env.planning.add_spike(story.id, "並び順は？")
    expected_remaining: list[str] = []
    if remaining == "same-story":
        expected_remaining.append(env.planning.add_spike(story.id, "件数は？").id)
    if remaining == "other-story-only":
        other = env.backlog.add_story(project, "別のストーリー")
        env.planning.add_spike(other.id, "別の質問")

    result = env.tools.answer_spike(spike.id, "新しい順")

    expected_refine = planner == "claude" and remaining != "same-story"
    [decision] = env.repo.get(Story, story.id).decisions
    assert "新しい順" in decision  # R-4: 回答を決定事項に追加する（書式は仕様に定めがない）
    assert result.decision == decision
    assert result.remaining_spikes == expected_remaining
    assert result.refine_started is expected_refine
    assert [argv for argv, _ in env.spawn.calls] == ([refine_argv(story.id)] if expected_refine else [])


@pytest.mark.parametrize(
    "spike_state",
    [
        pytest.param("answered", id="already-answered"),
        pytest.param("dropped", id="dropped"),
        pytest.param("missing", id="missing-id"),
        pytest.param("empty-id", id="empty-id"),
    ],
)
@pytest.mark.parametrize("planner", list(PLANNER_PROJECT))
def test_L10_21_answer_spike_not_open_fails_without_changes(env: Env, planner: str, spike_state: str) -> None:
    """プランナー設定 × 回答待ちでないスパイク（回答済み・取り下げ済み・存在しない ID・空の ID）の直積
    （L10〜L21）: 例外になり、決定事項は変わらず、見直しも起動しないこと。"""
    project = PLANNER_PROJECT[planner]
    story = put_sprint_in(env, "planning", project)
    assert story is not None
    spike_id = {"missing": "SP-99", "empty-id": ""}.get(spike_state, "")
    if spike_state in ("answered", "dropped"):
        spike = env.planning.add_spike(story.id, "並び順は？")
        spike_id = spike.id
        if spike_state == "answered":
            env.planning.answer_spike(spike.id, "古い順")
        else:
            env.planning.drop_spike(spike.id)
    before = env.repo.get(Story, story.id).decisions

    with pytest.raises(DomainError):
        env.tools.answer_spike(spike_id, "新しい順")

    assert env.repo.get(Story, story.id).decisions == before
    assert env.spawn.calls == []


def test_L22_answer_with_newline_and_non_ascii_is_kept_in_decision(env: Env) -> None:
    """回答が非 ASCII で改行を含むとき、回答はそのままの形でストーリーの決定事項に入ること。"""
    story = put_sprint_in(env, "planning")
    assert story is not None
    spike = env.planning.add_spike(story.id, "並び順は？")
    answer = "新しい順。\nただし固定記事は先頭 📌"

    env.tools.answer_spike(spike.id, answer)

    [decision] = env.repo.get(Story, story.id).decisions
    assert answer in decision
    assert env.repo.get(Spike, spike.id).status == SpikeStatus.ANSWERED


@pytest.mark.parametrize("planner", list(PLANNER_PROJECT))
def test_L23_25_answer_last_spike_of_backlog_story_follows_planner_condition(env: Env, planner: str) -> None:
    """スプリントに入っていない（バックログの）ストーリーで最後のスパイクに回答したとき、
    見直しを起動するのは AI プランナーが設定済みのときだけであること
    （CLI と同じ条件で、ストーリーの状態は見ない）。"""
    story = env.backlog.add_story(PLANNER_PROJECT[planner], "バックログのストーリー")
    spike = env.planning.add_spike(story.id, "並び順は？")

    result = env.tools.answer_spike(spike.id, "新しい順")

    expected_refine = planner == "claude"
    assert result.refine_started is expected_refine
    assert [argv for argv, _ in env.spawn.calls] == ([refine_argv(story.id)] if expected_refine else [])


# --- M. approve_plan(project) / reject_plan(project, comment) ---


@pytest.mark.parametrize(
    ("state", "project", "approves"),
    [
        pytest.param("none", P, False, id="M1-none"),
        pytest.param("planning", P, True, id="M2-planning-with-story"),
        pytest.param("planning-empty", P, False, id="M3-planning-without-story"),
        pytest.param("approved", P, False, id="M4-approved"),
        pytest.param("active", P, False, id="M5-active"),
        pytest.param("review", P, False, id="M6-review"),
        pytest.param("closed", P, False, id="M7-closed"),
        pytest.param("none", "nope", False, id="M8-missing-project"),
    ],
)
def test_M1_8_approve_plan_approves_only_planning_sprint_with_stories(
    env: Env, state: str, project: str, approves: bool
) -> None:
    """ストーリーが 1 件以上ある計画中のスプリントだけが承認済みになり、
    それ以外は例外で状態が変わらないこと。"""
    if state == "planning-empty":
        env.planning.new_sprint(P, "空のスプリント")
    else:
        put_sprint_in(env, state)
    before = sprint_state(env)

    if approves:
        env.tools.approve_plan(project)
        assert sprint_state(env)[0] == SprintStatus.APPROVED
    else:
        with pytest.raises(DomainError):
            env.tools.approve_plan(project)
        assert sprint_state(env) == before


COMMENTS = [
    pytest.param("ストーリーを減らしてほしい", True, id="ascii-free-text"),
    pytest.param("一覧は後回し。\n検索を先に 🔍", True, id="multiline-non-ascii"),
    pytest.param("", False, id="empty"),
    pytest.param(" \n\t", False, id="whitespace-only"),
]


@pytest.mark.parametrize(("comment", "comment_ok"), COMMENTS)
@pytest.mark.parametrize("state", STATES)
def test_M9_32_reject_plan_needs_comment_and_planning_or_approved_sprint(
    env: Env, state: str, comment: str, comment_ok: bool
) -> None:
    """S × コメントの直積（M9〜M32）: コメントが空白以外を含み、スプリントが計画中か承認済みのときだけ、
    計画中に戻ってコメントがそのまま残ること。それ以外は例外で、状態とコメントが変わらないこと。"""
    put_sprint_in(env, state)
    before = sprint_state(env)

    if comment_ok and state in ("planning", "approved"):
        env.tools.reject_plan(P, comment)
        assert sprint_state(env) == (SprintStatus.PLANNING, comment)
    else:
        with pytest.raises(DomainError):
            env.tools.reject_plan(P, comment)
        assert sprint_state(env) == before


def test_M33_reject_plan_of_missing_project_fails(env: Env) -> None:
    """存在しないプロジェクトを指定したとき、コメントがあっても例外になること。"""
    with pytest.raises(DomainError):
        env.tools.reject_plan("nope", "理由")


# --- N. list_spikes(project) ---


def test_N1_list_spikes_of_missing_project_fails(env: Env) -> None:
    """存在しないプロジェクトを指定したとき、例外になること。"""
    with pytest.raises(DomainError):
        env.tools.list_spikes("nope")


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param("no-stories", id="N2-no-stories"),
        pytest.param("stories-without-spikes", id="N3-stories-without-spikes"),
        pytest.param("closed-spikes-only", id="N4-answered-and-dropped-only"),
    ],
)
def test_N2_4_list_spikes_without_open_spikes_is_empty(env: Env, setup: str) -> None:
    """プロジェクトに未解決のスパイクがないとき（ストーリーがない・スパイクがない・回答済みと取り下げ済みだけ）、
    空のリストを返すこと。別のプロジェクトの未解決のスパイクは含めないこと。"""
    other = env.backlog.add_story(MANUAL, "別プロジェクト")
    env.planning.add_spike(other.id, "別プロジェクトの質問")
    if setup != "no-stories":
        story = env.backlog.add_story(P, "一覧")
        if setup == "closed-spikes-only":
            env.planning.answer_spike(env.planning.add_spike(story.id, "Q1").id, "A1")
            env.planning.drop_spike(env.planning.add_spike(story.id, "Q2").id)

    assert env.tools.list_spikes(P) == []


def test_N5_list_spikes_returns_open_spikes_of_all_stories_in_project(env: Env) -> None:
    """複数のストーリー（バックログと計画中のスプリント）に未解決のスパイクがあるとき、
    それらをすべて返し、回答済み・取り下げ済み・別のプロジェクトのスパイクは含めないこと。"""
    in_sprint = put_sprint_in(env, "planning")
    assert in_sprint is not None
    backlog = env.backlog.add_story(P, "バックログ")
    expected = {
        env.planning.add_spike(in_sprint.id, "並び順は？").id,
        env.planning.add_spike(backlog.id, "件数は？").id,
    }
    env.planning.drop_spike(env.planning.add_spike(backlog.id, "取り下げる").id)
    env.planning.add_spike(env.backlog.add_story(MANUAL, "別").id, "別プロジェクト")

    assert {s.id for s in env.tools.list_spikes(P)} == expected


# --- O. get_review(project) ---


@pytest.mark.parametrize(
    ("state", "project"),
    [
        pytest.param("none", P, id="O1-none"),
        pytest.param("planning", P, id="O2-planning"),
        pytest.param("approved", P, id="O3-approved"),
        pytest.param("active", P, id="O4-active"),
        pytest.param("closed", P, id="O5-closed"),
        pytest.param("none", "nope", id="O6-missing-project"),
    ],
)
def test_O1_6_get_review_outside_review_fails(env: Env, state: str, project: str) -> None:
    """スプリントがレビュー待ちでないとき（またはプロジェクトが存在しないとき）、例外になること。"""
    put_sprint_in(env, state)

    with pytest.raises(DomainError):
        env.tools.get_review(project)


GUIDE = ReviewGuideDraft(overview="記事の一覧が見られるようになった", steps=["caucus を起動する"])


@pytest.mark.parametrize(
    ("guide_available", "outcome"),
    [
        pytest.param(True, "done", id="O7-guide-done"),
        pytest.param(False, "done", id="O8-no-guide-done"),
        pytest.param(True, "failed", id="O9-guide-failed"),
        pytest.param(False, "suspended", id="O10-no-guide-suspended"),
    ],
)
def test_O7_10_get_review_returns_markdown_with_guide_only_when_planner_writes_it(
    env: Env, guide_available: bool, outcome: str
) -> None:
    """レビュー待ちのとき、ストーリーの結果によらず Markdown 本文と保存先を返し、
    確認手順は AI が書けたときだけ載ること（書けなくても資料は返る）。
    保存先のファイルの中身は本文と一致すること。"""
    story = put_sprint_in(env, "review", outcome=outcome)
    assert story is not None
    if guide_available:
        env.planner.review_guides.append(GUIDE)

    result = env.tools.get_review(P)

    assert f"{story.id}: {story.title}" in result.markdown
    assert (GUIDE.overview in result.markdown) is guide_available
    assert Path(result.path).read_text(encoding="utf-8") == result.markdown


def test_O11_get_review_twice_reuses_saved_guide(env: Env) -> None:
    """確認手順を一度書かせた後にもう一度取得したとき、AI に書かせ直さず（用意した確認手順は 1 つだけ）、
    同じ確認手順が載ること。"""
    put_sprint_in(env, "review")
    env.planner.review_guides.append(GUIDE)

    first = env.tools.get_review(P)
    second = env.tools.get_review(P)

    assert GUIDE.overview in first.markdown and GUIDE.overview in second.markdown


def test_O12_get_review_with_manual_planner_returns_markdown_without_guide(env: Env) -> None:
    """プランナーが manual（設定どおりのプランナー）のとき、確認手順なしでレビュー資料の本文と保存先を返すこと
    （README: AI プランナーが設定されていれば確認手順も載せる）。"""
    put_sprint_in(env, "review", project=EXPLICIT_MANUAL)
    tools = real_planner_tools(env)

    result = tools.get_review(EXPLICIT_MANUAL)

    assert "確認手順" not in result.markdown
    assert Path(result.path).read_text(encoding="utf-8") == result.markdown


# --- P. add_feedback(project, body, story_id) ---

OMIT = object()
STORY_KINDS = ["omitted", "none", "in-sprint", "backlog", "missing"]


def _feedback_count(env: Env) -> int:
    return len(env.repo.query(Feedback))


# none × in-sprint は作れない（スプリントがなければスプリント内のストーリーもない）ため除く
FEEDBACK_CASES = [
    pytest.param(state, kind, id=f"P{i}-{state}-story-{kind}")
    for i, (state, kind) in enumerate(
        ((st, k) for st in STATES for k in STORY_KINDS if not (st == "none" and k == "in-sprint")), 1
    )
]


@pytest.mark.parametrize(("state", "story_kind"), FEEDBACK_CASES)
def test_P1_29_add_feedback_only_during_review_and_for_story_in_the_sprint(
    env: Env, state: str, story_kind: str
) -> None:
    """S × ストーリーの指定の直積（P1〜P29。none × in-sprint は作れないので除く）: レビュー待ちのときだけ、
    ストーリーの指定なし（省略・None）かスプリント内のストーリーの指定で登録できること。
    それ以外は例外で、フィードバックは増えないこと。"""
    in_sprint = put_sprint_in(env, state)
    story_arg = {
        "omitted": OMIT,
        "none": None,
        "in-sprint": in_sprint.id if in_sprint else None,
        "backlog": env.backlog.add_story(P, "バックログ").id,
        "missing": "ST-999",
    }[story_kind]
    before = _feedback_count(env)

    def call() -> object:
        if story_arg is OMIT:
            return env.tools.add_feedback(P, "件数も表示したい")
        return env.tools.add_feedback(P, "件数も表示したい", story_arg)  # type: ignore[arg-type]

    if state == "review" and story_kind in ("omitted", "none", "in-sprint"):
        result = call()
        feedback = env.repo.get(Feedback, result.id)  # type: ignore[attr-defined]
        assert feedback.story_id == (None if story_arg is OMIT else story_arg)
        assert _feedback_count(env) == before + 1
    else:
        with pytest.raises(DomainError):
            call()
        assert _feedback_count(env) == before


def test_P30_add_feedback_keeps_multiline_non_ascii_body(env: Env) -> None:
    """本文が非 ASCII で改行を含むとき、そのままの形で登録されること。"""
    put_sprint_in(env, "review")
    body = "件数も表示したい。\n- 100 件を超えたら省略 ✂"

    result = env.tools.add_feedback(P, body)

    assert env.repo.get(Feedback, result.id).body == body


@pytest.mark.parametrize(
    "story_kind",
    [
        pytest.param("other-project", id="P31-story-of-other-project-sprint"),
        pytest.param("previous-closed-sprint", id="P32-story-of-previous-closed-sprint"),
    ],
)
def test_P31_32_add_feedback_for_story_outside_the_review_sprint_fails(env: Env, story_kind: str) -> None:
    """レビュー待ちでも、別のプロジェクトのスプリントのストーリーや、同じプロジェクトの終了済みの
    スプリントのストーリーを指定したとき、例外になり、フィードバックは増えないこと。"""
    if story_kind == "previous-closed-sprint":
        outside = put_sprint_in(env, "closed")
        put_sprint_in(env, "review")
    else:
        # 2 つのプロジェクトは同じ git リポジトリを使うため、別のプロジェクトは計画中までにする
        # （開始するとスプリントのブランチ名が重なる）
        outside = put_sprint_in(env, "planning", project=MANUAL)
        put_sprint_in(env, "review")
    assert outside is not None
    before = _feedback_count(env)

    with pytest.raises(DomainError):
        env.tools.add_feedback(P, "件数も表示したい", outside.id)

    assert _feedback_count(env) == before


# --- Q. complete_review(project) ---


@pytest.mark.parametrize(
    ("state", "project"),
    [
        pytest.param("none", P, id="Q1-none"),
        pytest.param("planning", P, id="Q2-planning"),
        pytest.param("approved", P, id="Q3-approved"),
        pytest.param("active", P, id="Q4-active"),
        pytest.param("closed", P, id="Q5-closed"),
        pytest.param("none", "nope", id="Q6-missing-project"),
    ],
)
def test_Q1_6_complete_review_outside_review_fails_without_changes(
    env: Env, state: str, project: str
) -> None:
    """スプリントがレビュー待ちでないとき、例外になり、スプリントの状態とバックログが変わらないこと。"""
    put_sprint_in(env, state)
    before = (sprint_state(env), [s.id for s in env.backlog.backlog(P)])

    with pytest.raises(DomainError):
        env.tools.complete_review(project)

    assert (sprint_state(env), [s.id for s in env.backlog.backlog(P)]) == before


def _assert_every_feedback_became_backlog_story(env: Env) -> None:
    """すべてのフィードバックが、フィードバックから作られたバックログのストーリーに変換されていること。"""
    backlog = {s.id: s for s in env.backlog.backlog(P)}
    for f in env.repo.query(Feedback):
        assert f.converted_story_id in backlog, f.id
        assert backlog[f.converted_story_id].source == StorySource.FEEDBACK


@pytest.mark.parametrize(
    "feedback",
    [
        pytest.param("none", id="Q7-no-feedback"),
        pytest.param("one-without-story", id="Q8-one-feedback"),
        pytest.param("two-one-with-story", id="Q9-two-feedback-one-with-story"),
        pytest.param("already-converted", id="Q10-already-converted"),
    ],
)
def test_Q7_10_complete_review_converts_every_pending_feedback_into_backlog_story(
    env: Env, feedback: str
) -> None:
    """レビュー待ちのとき、スプリントが終了し、すべてのフィードバックがバックログのストーリーに変換されること。
    フィードバックがなければストーリーは作られず、変換済みのフィードバックは二重に変換されない
    （変換先が変わらず、新しいストーリーも作られない）こと。"""
    story = put_sprint_in(env, "review")
    assert story is not None
    if feedback != "none":
        env.tools.add_feedback(P, "件数も表示したい")
    if feedback == "two-one-with-story":
        env.tools.add_feedback(P, "並び順を選びたい", story.id)
    converted_before = None
    if feedback == "already-converted":
        converted_before = env.injector.get(ReviewService).convert_feedback(env.repo.query(Feedback)[0].id).id

    env.tools.complete_review(P)

    assert sprint_state(env)[0] == SprintStatus.CLOSED
    _assert_every_feedback_became_backlog_story(env)
    from_feedback = [s for s in env.backlog.backlog(P) if s.source == StorySource.FEEDBACK]
    if feedback == "none":
        assert from_feedback == []
    if feedback == "already-converted":
        assert [s.id for s in from_feedback] == [converted_before]
        assert env.repo.query(Feedback)[0].converted_story_id == converted_before


@pytest.mark.parametrize(
    ("outcome", "returns_to_backlog"),
    [
        pytest.param("done", False, id="Q11-done"),
        pytest.param("failed", True, id="Q12-failed"),
        pytest.param("suspended", True, id="Q13-suspended"),
    ],
)
def test_Q11_13_complete_review_returns_unfinished_stories_to_backlog(
    env: Env, outcome: str, returns_to_backlog: bool
) -> None:
    """レビュー待ちのとき、失敗・中断したストーリーはバックログに戻り（スプリントから外れる）、
    完了したストーリーは完了のまま残ること（R-6）。"""
    story = put_sprint_in(env, "review", outcome=outcome)
    assert story is not None

    env.tools.complete_review(P)

    after = env.repo.get(Story, story.id)
    if returns_to_backlog:
        assert (after.status, after.sprint_id) == (StoryStatus.BACKLOG, None)
    else:
        assert (after.status, after.sprint_id) == (StoryStatus.DONE, story.sprint_id)


def test_Q14_complete_review_adopts_ai_conversion_when_planner_succeeds(env: Env) -> None:
    """AI プランナーがフィードバックの変換に成功したとき、AI がまとめたストーリーがバックログに入り、
    元になったフィードバックはそのストーリーに変換済みになること
    （README: 未変換のフィードバックを AI が変換する）。"""
    put_sprint_in(env, "review")
    first = env.tools.add_feedback(P, "件数も表示したい")
    second = env.tools.add_feedback(P, "並び順を選びたい")
    env.planner.conversions.append(
        FeedbackConversionDraft(
            stories=[FeedbackStoryDraft(feedback_ids=[first.id, second.id], title="一覧の表示を改善する")]
        )
    )

    env.tools.complete_review(P)

    assert sprint_state(env)[0] == SprintStatus.CLOSED
    [story] = [s for s in env.backlog.backlog(P) if s.source == StorySource.FEEDBACK]
    assert story.title == "一覧の表示を改善する"
    assert {f.converted_story_id for f in env.repo.query(Feedback)} == {story.id}


def test_Q15_complete_review_with_manual_planner_still_converts_feedback(env: Env) -> None:
    """プランナーが manual（設定どおりのプランナー）のときも、スプリントが終了し、
    フィードバックはバックログのストーリーに変換されること。"""
    put_sprint_in(env, "review", project=EXPLICIT_MANUAL)
    tools = real_planner_tools(env)
    tools.add_feedback(EXPLICIT_MANUAL, "件数も表示したい")

    tools.complete_review(EXPLICIT_MANUAL)

    assert sprint_state(env, EXPLICIT_MANUAL)[0] == SprintStatus.CLOSED
    backlog = {s.id: s for s in env.backlog.backlog(EXPLICIT_MANUAL)}
    [feedback] = env.repo.query(Feedback)
    assert backlog[feedback.converted_story_id].source == StorySource.FEEDBACK


# --- R. BackgroundRunner.start_refine(project_name, story_id) ---


@pytest.mark.parametrize(
    "project_name",
    [
        pytest.param("demo", id="R1-ascii"),
        pytest.param("記事 サイト", id="R2-non-ascii-with-space"),
        pytest.param("../escape", id="R3-parent-dir"),
        pytest.param("a/b", id="R4-slash"),
    ],
)
def test_R1_4_refine_log_stays_directly_under_refine_dir(tmp_path: Path, project_name: str) -> None:
    """プロジェクト名がどんな文字を含んでも、見直しのログは CAUCUS_HOME/logs/refine/ の直下に作られ
    （README の記載）、起動するのはそのストーリーの見直しであること。"""
    spawn = FakeSpawn()
    log_dir = tmp_path / "logs"
    runner = BackgroundRunner(tmp_path / "run", log_dir, spawn)

    launched = runner.start_refine(project_name, "ST-1")

    [(argv, log_path)] = spawn.calls
    assert argv == refine_argv("ST-1")
    assert log_path == launched.log_path
    assert log_path.parent == log_dir / "refine"


@pytest.mark.parametrize(
    "story_id",
    [
        pytest.param("a/b", id="R5-story-id-with-slash"),
        pytest.param("../x", id="R6-story-id-with-parent-dir"),
    ],
)
def test_R5_6_refine_log_stays_under_refine_dir_for_any_story_id(tmp_path: Path, story_id: str) -> None:
    """ストーリーの ID に `/` や `..` が含まれても、見直しのログは logs/refine/ の直下に作られること。"""
    spawn = FakeSpawn()
    log_dir = tmp_path / "logs"
    runner = BackgroundRunner(tmp_path / "run", log_dir, spawn)

    launched = runner.start_refine(P, story_id)

    assert launched.log_path.parent == log_dir / "refine"


def test_R7_refine_command_receives_story_id_starting_with_dash_as_story_id(tmp_path: Path) -> None:
    """ストーリーの ID が `-` で始まるとき、起動する見直しのコマンド（本物の CLI）は、それをオプションではなく
    ストーリーの ID として受け取ること。存在しない ID なので「<ID> が見つかりません」で終わる
    （オプションと解釈されると「No such option」になる）。"""
    spawn = FakeSpawn()
    runner = BackgroundRunner(tmp_path / "run", tmp_path / "logs", spawn)
    runner.start_refine(P, "-x")
    [(argv, _)] = spawn.calls

    done = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=60,
        env={"CAUCUS_HOME": str(tmp_path / "home"), "PATH": os.environ.get("PATH", "")},
    )

    assert done.returncode != 0
    assert "-x が見つかりません" in done.stderr
