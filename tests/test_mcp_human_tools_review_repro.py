"""レビューで指摘された ST-3 の不具合の再現テスト。

どのテストも、指摘された条件をそのまま作り、正しい振る舞いを期待値にしている（修正前は落ちる）。
モックするのはプロセスの起動（spawn）だけ。計画ジョブの実行中は、計画ジョブのロックを持つことで作る
（`PlanJobs` の起動器を、ロックを持ち続けるだけのものにする。プロセス境界）。
"""

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from caucus_race.domain.entities import Spike, Story
from caucus_race.domain.errors import DomainError
from caucus_race.domain.status import SpikeStatus, SprintStatus
from caucus_race.plan_job import PlanJobs
from caucus_race.runner import BackgroundRunner, RunLock, spawn_detached
from tests.test_mcp_human_tools import Env, P
from tests.test_mcp_human_tools_combinations import put_sprint_in, sprint_state


@pytest.fixture
def env(tmp_path: Path, git_repo: Path) -> Env:
    return Env(tmp_path, git_repo)


@pytest.fixture
def unrelated_process() -> Iterator[subprocess.Popen[bytes]]:
    """caucus と無関係な、生きているプロセス（OS が終わったプロセスの pid を再利用した先の代わり）。"""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(10)


def finished_run_whose_pid_was_reused(env: Env, reused_pid: int) -> None:
    """実行プロセスを起動し、そのプロセスが実行ロックを取ってスプリントを実行し、終わった状態を作る。
    その後、OS がその pid を無関係なプロセス（reused_pid）に再利用した、という状況にする。"""
    runner = env.injector.get(BackgroundRunner)
    runner.spawn = lambda argv, log_path: reused_pid  # 起動したプロセスの pid が、後で再利用される pid と同じ
    env.tools.resume_sprint(P)
    runner.spawn = env.spawn
    time.sleep(0.05)  # 起動の後に、実行プロセスがロックを取った（ファイルの更新時刻が後になる）
    with RunLock(env.settings.run_dir, P).hold():
        pass  # 実行プロセスがロックを取り、実行を終えて手放した


# --- 起動印（.launched）が残り、pid の再利用で誤判定する（/code-review: runner.py:155） ---


def test_resume_is_accepted_after_run_finished_even_if_its_pid_was_reused(
    env: Env, unrelated_process: subprocess.Popen[bytes]
) -> None:
    """前回起動した実行プロセスがロックを取って実行を終えた後、
    その pid が無関係なプロセスに再利用されていても、resume_sprint は「既に実行中」で拒否せず、
    実行プロセスを起動すること。"""
    put_sprint_in(env, "active")
    finished_run_whose_pid_was_reused(env, unrelated_process.pid)

    result = env.tools.resume_sprint(P)

    assert result.pid == env.spawn.pid
    assert len(env.spawn.calls) == 1


def test_stop_does_not_signal_unrelated_process_that_reused_finished_runs_pid(
    env: Env, unrelated_process: subprocess.Popen[bytes]
) -> None:
    """前回の実行プロセスが終わった後、その pid が無関係なプロセスに再利用されていても、stop_sprint は
    そのプロセスに停止を送らず、「実行されていません」になること。"""
    put_sprint_in(env, "active")
    finished_run_whose_pid_was_reused(env, unrelated_process.pid)

    with pytest.raises(DomainError, match="実行されていません"):
        env.tools.stop_sprint(P)

    time.sleep(0.2)
    assert unrelated_process.poll() is None, "無関係なプロセスが止められました"


# --- 計画ジョブの実行中に、承認・差し戻し・見直しを受け付ける
# （/code-review: ai_planning.py:175・human_tools.py:132） ---


@pytest.fixture
def plan_job_running(env: Env) -> Iterator[None]:
    """AI の計画ジョブを実行中にする（ジョブのロックを持ち続けるだけで、計画は作らない）。"""
    held: list[int] = []
    jobs = env.injector.get(PlanJobs)
    jobs.launcher = lambda argv, log_path, lock_fd: held.append(os.dup(lock_fd))
    jobs.start(P)
    assert jobs.running(P)
    try:
        yield
    finally:
        for fd in held:
            os.close(fd)


@pytest.mark.parametrize("state", ["planning", "approved"])
def test_reject_plan_while_ai_is_planning_is_rejected_and_keeps_state(
    env: Env, plan_job_running: None, state: str
) -> None:
    """AI がスプリント計画を作成中のとき、reject_plan はエラーになり、スプリントの状態と差し戻しコメントは
    変わらないこと
    （作成中は計画を変更するツールはエラーになる。完了時に AI の計画で黙って上書きされないため）。"""
    put_sprint_in(env, state)
    before = sprint_state(env)

    with pytest.raises(DomainError, match="作成中"):
        env.tools.reject_plan(P, "認証を先にしてほしい")

    assert sprint_state(env) == before


def test_approve_plan_while_ai_is_planning_is_rejected_and_keeps_state(
    env: Env, plan_job_running: None
) -> None:
    """AI がスプリント計画を作成中のとき、approve_plan はエラーになり、スプリントは計画中のままであること
    （作成途中で置き換えられる計画を承認させない）。"""
    put_sprint_in(env, "planning")

    with pytest.raises(DomainError, match="作成中"):
        env.tools.approve_plan(P)

    assert sprint_state(env)[0] == SprintStatus.PLANNING


def test_answer_spike_while_ai_is_planning_records_answer_without_starting_refine(
    env: Env, plan_job_running: None
) -> None:
    """AI がスプリント計画を作成中のとき、最後のスパイクに回答しても、回答は決定事項に記録され、
    同じストーリーを書き換える見直しを並行して起動しないこと（refine_started は False）。"""
    story = env.backlog.add_story(P, "一覧画面")
    spike = env.planning.add_spike(story.id, "並び順は？")

    result = env.tools.answer_spike(spike.id, "新しい順")

    assert env.repo.get(Spike, spike.id).status == SpikeStatus.ANSWERED
    assert any("新しい順" in d for d in env.repo.get(Story, story.id).decisions)
    assert result.refine_started is False
    assert env.spawn.calls == []


# --- 起動に失敗したときの返し方（/code-review: human_tools.py:148・human_tools.py:185） ---


def use_real_spawn_with_unwritable_log_dir(env: Env) -> None:
    env.injector.get(BackgroundRunner).spawn = spawn_detached
    env.settings.log_dir.parent.mkdir(parents=True, exist_ok=True)
    env.settings.log_dir.write_text("ディレクトリではない", encoding="utf-8")


def test_answer_spike_refine_launch_failure_still_reports_recorded_answer(env: Env) -> None:
    """最後のスパイクに回答し、見直しのプロセスを起動できなかったとき、回答は記録済みなので、エラーではなく
    回答の結果を返し（エージェントが再試行して「回答待ちではありません」にならない）、見直しは始まっていない
    （refine_started は False）ことと、起動できなかったことをメッセージで伝えること。"""
    story = env.backlog.add_story(P, "一覧画面")
    spike = env.planning.add_spike(story.id, "並び順は？")
    use_real_spawn_with_unwritable_log_dir(env)

    result = env.tools.answer_spike(spike.id, "新しい順")

    assert env.repo.get(Spike, spike.id).status == SpikeStatus.ANSWERED
    assert result.refine_started is False
    assert "起動できませんでした" in result.message


def test_start_sprint_launch_failure_tells_how_to_recover_with_resume(env: Env) -> None:
    """開始を確定した後に実行プロセスを起動できなかったとき、エラーのメッセージで、スプリントは開始済みであり
    resume_sprint で起動し直せることを伝えること
    （start_sprint を再試行すると「承認されていません」になるため）。"""
    put_sprint_in(env, "approved")
    use_real_spawn_with_unwritable_log_dir(env)

    with pytest.raises(DomainError, match="resume_sprint"):
        env.tools.start_sprint(P)
