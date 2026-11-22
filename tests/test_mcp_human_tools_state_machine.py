"""人間の判断にあたる操作のツールから見た、スプリントの状態遷移テスト（ST-3）。

状態遷移表と遷移の列は docs/test-design/20261123-caucus-sprint-1.md の「## 状態遷移テスト」の
「ST-3: スプリントの状態 × 実行プロセス」にある。

状態は、スプリントの状態（DB）と、実行プロセスがあるか（実行ロックを持つプロセスがいるか）の組で表す。
- RUNNING の「実行プロセス」は、実行ロックを持って眠るだけの本物の子プロセスで代用する
  （stop_sprint で実際に止められる）
- start_sprint / resume_sprint が起動する実行プロセスは、偽の起動（FakeSpawn。プロセス境界）で記録だけする

無効な遷移では、例外（DomainError）に加えて、状態（スプリントの状態・差し戻しコメント・実行プロセスの有無・
フィードバックの件数・ストーリーの状態）が変わっていないこと、プロセスを起動していないことを確かめる。
"""

import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from caucus_race.domain.entities import Feedback, Sprint, Story
from caucus_race.domain.errors import DomainError
from caucus_race.domain.status import SprintStatus, StoryStatus
from caucus_race.orchestrator import Orchestrator
from caucus_race.runner import BackgroundRunner, RunLock, runner_argv, spawn_detached
from caucus_race.usecase.common import latest_sprint, project_by_name
from tests.test_mcp_human_tools import Env, FakeSpawn, P
from tests.test_mcp_human_tools_combinations import put_sprint_in

NONE = "NONE"
PLANNING = "PLANNING"
APPROVED = "APPROVED"
RUNNING = "RUNNING"
LAUNCHING = "LAUNCHING"
STOPPED = "STOPPED"
REVIEW = "REVIEW"
CLOSED = "CLOSED"
STATES = [NONE, PLANNING, APPROVED, LAUNCHING, RUNNING, STOPPED, REVIEW, CLOSED]
# 実行プロセスがいる状態（無効な遷移で、そのプロセスが止まらないことも確かめる）
WITH_PROCESS = (LAUNCHING, RUNNING)

DB_STATUS = {PLANNING: "planning", APPROVED: "approved", REVIEW: "review", CLOSED: "closed"}

EVENTS = ["approve", "reject", "start", "stop", "resume", "get_review", "add_feedback", "complete"]


@pytest.fixture
def env(tmp_path: Path, git_repo: Path) -> Env:
    return Env(tmp_path, git_repo)


# --- 実行プロセス（本物の子プロセス） ---


@contextmanager
def execution_process(env: Env) -> Iterator[subprocess.Popen[bytes]]:
    """実行ロックを持って眠るだけの子プロセス（スプリントを実行中のプロセスの代わり）を起動し、ロックを取るまで待つ。"""
    script = (
        "import time; from pathlib import Path; from caucus_race.runner import RunLock\n"
        f"with RunLock(Path({str(env.settings.run_dir)!r}), {P!r}).hold():\n"
        "    time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        while RunLock(env.settings.run_dir, P).running() is None:
            assert proc.poll() is None, "子プロセスが終わってしまいました"
            assert time.monotonic() < deadline, "子プロセスがロックを取りませんでした"
            time.sleep(0.05)
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(10)


class SleepingSpawn:
    """起動したがまだ実行ロックを取っていない実行プロセスの代わり。眠るだけの本物のプロセスを
    新しいプロセスグループで起動する（本物の起動と同じく、停止はプロセスグループに送られる）。"""

    def __init__(self) -> None:
        self.procs: list[subprocess.Popen[bytes]] = []

    def __call__(self, argv: list[str], log_path: Path) -> int:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.procs.append(proc)
        return proc.pid

    def kill_all(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait(10)


@contextmanager
def launching_process(env: Env) -> Iterator[subprocess.Popen[bytes]]:
    """STOPPED（active・実行プロセスなし）から、実行プロセスを起動した直後（まだロックを取っていない）の状態を作る。
    起動の後は偽の起動に戻すので、テスト中の env.spawn.calls は「この後に起動したか」だけを表す。"""
    runner = env.injector.get(BackgroundRunner)
    sleeping = SleepingSpawn()
    runner.spawn = sleeping
    try:
        runner.start_run(P)
        runner.spawn = env.spawn
        yield sleeping.procs[0]
    finally:
        runner.spawn = env.spawn
        sleeping.kill_all()


def wait_until_stopped(env: Env, proc: subprocess.Popen[bytes]) -> None:
    proc.wait(10)
    deadline = time.monotonic() + 10
    while RunLock(env.settings.run_dir, P).running() is not None:
        assert time.monotonic() < deadline, "停止したのに実行ロックが残っています"
        time.sleep(0.05)


# --- 状態の観測 ---


@dataclass(frozen=True)
class Observed:
    sprint: str
    plan_comment: str | None
    running: bool
    feedback: int
    stories: tuple[tuple[str, str, str | None], ...]


def observe(env: Env) -> Observed:
    sprint = latest_sprint(env.repo, project_by_name(env.repo, P))
    return Observed(
        sprint=sprint.status if sprint else "none",
        plan_comment=sprint.plan_comment if sprint else None,
        running=env.injector.get(BackgroundRunner).running(P) is not None,
        feedback=len(env.repo.query(Feedback)),
        stories=tuple(sorted((s.id, s.status, s.sprint_id) for s in env.repo.query(Story))),
    )


def setup_state(env: Env, state: str) -> None:
    """RUNNING 以外の状態を作る。RUNNING は STOPPED を作ってから execution_process で実行プロセスを足す。"""
    db_state = {
        NONE: "none",
        PLANNING: "planning",
        APPROVED: "approved",
        RUNNING: "active",
        LAUNCHING: "active",
        STOPPED: "active",
        REVIEW: "review",
        CLOSED: "closed",
    }[state]
    put_sprint_in(env, db_state)


def fire(env: Env, event: str) -> object:
    actions: dict[str, Callable[[], object]] = {
        "approve": lambda: env.tools.approve_plan(P),
        "reject": lambda: env.tools.reject_plan(P, "ストーリーを減らしてほしい"),
        "start": lambda: env.tools.start_sprint(P),
        "stop": lambda: env.tools.stop_sprint(P),
        "resume": lambda: env.tools.resume_sprint(P),
        "get_review": lambda: env.tools.get_review(P),
        "add_feedback": lambda: env.tools.add_feedback(P, "件数も表示したい"),
        "complete": lambda: env.tools.complete_review(P),
    }
    return actions[event]()


# --- 状態遷移表 ---

# 有効な遷移: (状態, イベント) → 遷移先
VALID = {
    (PLANNING, "approve"): APPROVED,
    (PLANNING, "reject"): PLANNING,
    (APPROVED, "reject"): PLANNING,
    (APPROVED, "start"): RUNNING,
    (RUNNING, "stop"): STOPPED,
    (STOPPED, "resume"): RUNNING,
    (REVIEW, "get_review"): REVIEW,
    (REVIEW, "add_feedback"): REVIEW,
    (REVIEW, "complete"): CLOSED,
}
VALID[(LAUNCHING, "stop")] = STOPPED
# 仕様不明: 停止中でない（active 以外の）スプリントの resume_sprint
UNKNOWN = {(s, "resume") for s in (NONE, PLANNING, APPROVED, REVIEW, CLOSED)}
INVALID = [(s, e) for s in STATES for e in EVENTS if (s, e) not in VALID and (s, e) not in UNKNOWN]


NO_OPEN_SPRINT = "進行中のスプリントがありません"


def reason(state: str, event: str) -> str:
    """無効な遷移で、例外のメッセージに含まれるべき理由（状態遷移表の各セルの理由）。"""
    if event == "stop":
        return "実行されていません"
    if event in ("start", "resume") and state in WITH_PROCESS:
        return "既に実行中"
    if state in (NONE, CLOSED):
        return NO_OPEN_SPRINT
    if event == "start":
        return "承認されていません"  # R-2
    if event == "approve":
        return "計画を変更できません"  # R-3
    if event == "reject":
        return "は (active|review) です"
    return "レビュー待ちではありません"  # get_review・add_feedback・complete


@pytest.mark.parametrize(("state", "event"), [pytest.param(s, e, id=f"{s}-{e}") for s, e in INVALID])
def test_invalid_transition_raises_and_keeps_state(env: Env, state: str, event: str) -> None:
    """無効な遷移（状態遷移表で「例外」としたセル）のとき、そのセルの理由を含む DomainError になり、
    スプリントの状態・差し戻しコメント・実行プロセスの有無・フィードバック・ストーリーが変わらず、
    プロセスも起動しないこと。実行プロセスがいる状態（起動直後・実行中）では、そのプロセスも止まらないこと。"""
    setup_state(env, state)

    if state in WITH_PROCESS:
        process = launching_process(env) if state == LAUNCHING else execution_process(env)
        with process as proc:
            before = observe(env)
            with pytest.raises(DomainError, match=reason(state, event)):
                fire(env, event)
            after = observe(env)
            assert proc.poll() is None, "実行中のプロセスが止まってしまいました"
    else:
        before = observe(env)
        with pytest.raises(DomainError, match=reason(state, event)):
            fire(env, event)
        after = observe(env)

    assert after == before
    assert env.spawn.calls == []


@pytest.mark.parametrize(
    ("state", "event"),
    [pytest.param(s, e, id=f"{s}-{e}") for (s, e) in VALID if s not in WITH_PROCESS],
)
def test_valid_transition_reaches_expected_state(env: Env, state: str, event: str) -> None:
    """有効な遷移（RUNNING からの停止以外）のとき、状態遷移表の遷移先になること。
    RUNNING への遷移は、スプリントが active になり、
    そのプロジェクトの実行プロセスを 1 回起動したことで表す。"""
    setup_state(env, state)
    before = observe(env)

    fire(env, event)

    after = observe(env)
    expected = VALID[(state, event)]
    if expected == RUNNING:
        assert after.sprint == SprintStatus.ACTIVE
        [(argv, log_path)] = env.spawn.calls
        assert argv == runner_argv(P, log_path)
    else:
        assert after.sprint == DB_STATUS[expected]
        assert env.spawn.calls == []
    if (state, event) == (REVIEW, "add_feedback"):
        assert after.feedback == before.feedback + 1
    if (state, event) == (REVIEW, "get_review"):
        assert after == before  # 参照だけで、状態を変えない
    if event == "reject":
        assert after.plan_comment == "ストーリーを減らしてほしい"


def test_RUNNING_stop_reaches_STOPPED(env: Env) -> None:
    """RUNNING で stop_sprint を呼ぶと、実行プロセスが止まり、
    スプリントは active のまま（STOPPED）になること。"""
    setup_state(env, RUNNING)

    with execution_process(env) as proc:
        env.tools.stop_sprint(P)
        wait_until_stopped(env, proc)

    after = observe(env)
    assert (after.sprint, after.running) == (SprintStatus.ACTIVE, False)


def test_LAUNCHING_stop_reaches_STOPPED(env: Env) -> None:
    """起動した直後でまだ実行ロックを取っていない実行プロセスがいるとき、stop_sprint はそのプロセスを止め、
    スプリントは active のまま（STOPPED）になること（受け入れ条件: 実行中のプロセスに停止を指示する）。"""
    setup_state(env, STOPPED)

    with launching_process(env) as proc:
        env.tools.stop_sprint(P)
        proc.wait(10)

    after = observe(env)
    assert (after.sprint, after.running) == (SprintStatus.ACTIVE, False)


def test_STOPPED_after_interrupt_resume_launches(env: Env) -> None:
    """実行の途中で止めた（実行中だったストーリーが running のまま残った）STOPPED でも、resume_sprint は
    実行プロセスを起動すること
    （README: 実行中だったストーリーは resume で再実行される。再実行は実行プロセスが行う）。"""
    setup_state(env, STOPPED)
    story = env.repo.query(Story)[0]
    story.status = StoryStatus.RUNNING  # SIGTERM で止まったときに残る状態
    env.repo.save(story)

    env.tools.resume_sprint(P)

    [(argv, log_path)] = env.spawn.calls
    assert argv == runner_argv(P, log_path)
    assert observe(env).sprint == SprintStatus.ACTIVE


# --- 重要な遷移の列 ---


def test_sequence_reject_then_reapprove_then_start(env: Env) -> None:
    """承認 → 差し戻し → 再承認 → 開始 の順に呼んだとき、各段階で approved → planning（コメントあり）→
    approved（コメントは消える）→ active（実行プロセスを 1 回起動）になること。"""
    setup_state(env, PLANNING)

    env.tools.approve_plan(P)
    assert (observe(env).sprint, observe(env).plan_comment) == (SprintStatus.APPROVED, None)
    env.tools.reject_plan(P, "やり直し")
    assert (observe(env).sprint, observe(env).plan_comment) == (SprintStatus.PLANNING, "やり直し")
    env.tools.approve_plan(P)
    assert (observe(env).sprint, observe(env).plan_comment) == (SprintStatus.APPROVED, None)
    env.tools.start_sprint(P)
    assert observe(env).sprint == SprintStatus.ACTIVE
    assert len(env.spawn.calls) == 1


def test_sequence_start_double_start_stop_resume_double_resume(env: Env) -> None:
    """開始 → （実行中に）二重開始 → 停止 → 再開 → （実行中に）二重再開 の順に呼んだとき、
    二重の開始・再開は例外で実行プロセスを増やさず、停止後の再開でちょうど 1 回起動し直すこと。"""
    setup_state(env, APPROVED)

    env.tools.start_sprint(P)
    assert len(env.spawn.calls) == 1
    with execution_process(env) as proc:  # 起動した実行プロセスがロックを取った状態
        with pytest.raises(DomainError, match="既に実行中"):
            env.tools.start_sprint(P)
        assert len(env.spawn.calls) == 1
        env.tools.stop_sprint(P)
        wait_until_stopped(env, proc)
    assert (observe(env).sprint, observe(env).running) == (SprintStatus.ACTIVE, False)

    env.tools.resume_sprint(P)
    assert len(env.spawn.calls) == 2
    with execution_process(env):
        with pytest.raises(DomainError, match="既に実行中"):
            env.tools.resume_sprint(P)
        assert len(env.spawn.calls) == 2


def test_sequence_stop_twice(env: Env) -> None:
    """実行中に停止 → もう一度停止 の順に呼んだとき、2 回目は例外「実行されていません」になり、
    スプリントは active のまま（STOPPED のまま）であること。"""
    setup_state(env, RUNNING)
    with execution_process(env) as proc:
        env.tools.stop_sprint(P)
        wait_until_stopped(env, proc)
    before = observe(env)

    with pytest.raises(DomainError, match="実行されていません"):
        env.tools.stop_sprint(P)

    assert observe(env) == before


def test_sequence_full_lifecycle_and_operations_after_close(env: Env) -> None:
    """計画中 → 承認 → 開始 → 実行の完了 → レビュー資料の取得 → フィードバック → レビュー完了 の順に
    進めたとき、    各段階で planning → approved → active → review → review → review → closed になること。
    終了後のレビュー完了・開始・フィードバックは例外で、フィードバックは二重に変換されないこと。"""
    setup_state(env, PLANNING)

    env.tools.approve_plan(P)
    assert observe(env).sprint == SprintStatus.APPROVED
    env.tools.start_sprint(P)
    assert observe(env).sprint == SprintStatus.ACTIVE
    # 実行プロセスがスプリントを最後まで実行した（起動した子プロセスの代わりに、同じ処理をこの場で行う）
    sprint = latest_sprint(env.repo, project_by_name(env.repo, P))
    assert sprint is not None
    env.injector.get(Orchestrator).run(sprint.id)
    assert observe(env).sprint == SprintStatus.REVIEW
    env.tools.get_review(P)
    assert observe(env).sprint == SprintStatus.REVIEW
    env.tools.add_feedback(P, "件数も表示したい")
    assert observe(env).sprint == SprintStatus.REVIEW
    env.tools.complete_review(P)
    assert observe(env).sprint == SprintStatus.CLOSED
    after_close = (observe(env), len(env.backlog.backlog(P)))

    for event in ("complete", "start", "add_feedback"):
        with pytest.raises(DomainError):
            fire(env, event)
    assert (observe(env), len(env.backlog.backlog(P))) == after_close


def test_sequence_start_fails_then_succeeds_after_fix(env: Env, git_repo: Path) -> None:
    """開始の条件を満たさず失敗（未コミットの変更）→ 原因を取り除く → もう一度開始 の順に呼んだとき、
    失敗の後も承認済みのまま（もう一度開始できる）で、2 回目に開始して実行プロセスを 1 回だけ起動すること。"""
    setup_state(env, APPROVED)
    dirty = git_repo / "untracked.txt"
    dirty.write_text("未コミット", encoding="utf-8")

    with pytest.raises(DomainError, match="未コミットの変更"):
        env.tools.start_sprint(P)
    assert observe(env).sprint == SprintStatus.APPROVED

    dirty.unlink()
    env.tools.start_sprint(P)
    assert observe(env).sprint == SprintStatus.ACTIVE
    assert len(env.spawn.calls) == 1


def test_sequence_resume_launch_fails_then_succeeds_after_fix(env: Env) -> None:
    """停止中の再開で実行プロセスを起動できず失敗 → 原因を取り除く → もう一度再開 の順に呼んだとき、
    失敗の後も停止中のまま（active・ストーリーは queued）で、2 回目に起動できること。"""
    setup_state(env, STOPPED)
    runner = env.injector.get(BackgroundRunner)
    runner.spawn = spawn_detached
    env.settings.log_dir.parent.mkdir(parents=True, exist_ok=True)
    env.settings.log_dir.write_text("ディレクトリではない", encoding="utf-8")

    with pytest.raises(DomainError, match="プロセスを起動できませんでした"):
        env.tools.resume_sprint(P)
    after_failure = observe(env)
    assert (after_failure.sprint, after_failure.running) == (SprintStatus.ACTIVE, False)
    assert {status for _, status, _ in after_failure.stories} == {StoryStatus.QUEUED}

    env.settings.log_dir.unlink()
    retry = FakeSpawn()
    runner.spawn = retry
    env.tools.resume_sprint(P)
    [(argv, log_path)] = retry.calls
    assert argv == runner_argv(P, log_path)
    assert env.repo.query(Sprint)[0].status == SprintStatus.ACTIVE


def _active_with_launching_process(env: Env, how: str) -> SleepingSpawn:
    """開始（how="start"）または再開（how="resume"）で、ロックを取る前の実行プロセスを起動した状態を作る。"""
    setup_state(env, APPROVED if how == "start" else STOPPED)
    sleeping = SleepingSpawn()
    env.injector.get(BackgroundRunner).spawn = sleeping
    fire(env, how)
    assert observe(env).sprint == SprintStatus.ACTIVE
    assert len(sleeping.procs) == 1
    return sleeping


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param("start", "resume", id="SQ7-start-then-resume"),
        pytest.param("start", "start", id="SQ8-start-then-start"),
        pytest.param("resume", "resume", id="SQ9-resume-then-resume"),
    ],
)
def test_sequence_second_launch_right_after_launch_is_rejected(env: Env, first: str, second: str) -> None:
    """開始・再開の直後（起動した実行プロセスがまだ実行ロックを取っていない間）にもう一度開始・再開を呼んだとき、
    例外「既に実行中」になり、実行プロセスを二重に起動しないこと
    （受け入れ条件: 二重に起動せず、理由を説明する）。"""
    sleeping = _active_with_launching_process(env, first)
    try:
        with pytest.raises(DomainError, match="既に実行中"):
            fire(env, second)
        assert len(sleeping.procs) == 1
        assert sleeping.procs[0].poll() is None
    finally:
        sleeping.kill_all()


def test_sequence_launched_process_dies_before_lock_then_resume_launches(env: Env) -> None:
    """開始の直後、起動した実行プロセスがロックを取らずに終わった（起動に失敗した）とき、
    その後の再開は受け付けられ、実行プロセスを起動し直すこと（終わったプロセスを実行中とみなし続けない）。"""
    sleeping = _active_with_launching_process(env, "start")
    sleeping.kill_all()
    retry = FakeSpawn()
    env.injector.get(BackgroundRunner).spawn = retry

    env.tools.resume_sprint(P)

    assert len(retry.calls) == 1


def test_sequence_stop_right_after_start_then_resume(env: Env) -> None:
    """開始の直後（ロックを取る前）に停止 → 再開 の順に呼んだとき、停止で起動したプロセスが
    止まって STOPPED になり、    再開で実行プロセスを起動し直すこと。"""
    sleeping = _active_with_launching_process(env, "start")
    try:
        env.tools.stop_sprint(P)
        sleeping.procs[0].wait(10)
        assert (observe(env).sprint, observe(env).running) == (SprintStatus.ACTIVE, False)

        retry = FakeSpawn()
        env.injector.get(BackgroundRunner).spawn = retry
        env.tools.resume_sprint(P)
        assert len(retry.calls) == 1
    finally:
        sleeping.kill_all()
