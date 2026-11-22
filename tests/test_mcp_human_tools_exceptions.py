"""人間の判断にあたる操作のツール（HumanTools）・BackgroundRunner・MCP の層・CLI の例外テスト（ST-3）。

例外表は docs/test-design/20261123-caucus-sprint-1.md の「## 例外テスト」の XH1〜XH35 にある。

例外の型とメッセージの要点（理由を表す語句）を `pytest.raises(型, match=...)` で確かめ、例外の後の状態
（スプリント・ストーリー・ブランチ・ロック・フィードバック・ファイル）が変わっていないことを確かめる。

外部の失敗は、本物のプロセス境界で起こす（git の失敗・ログのディレクトリを作れない）。
プロセスの起動を偽物にするのは、プロセスを起動しない（起動の前に拒否する）ことを確かめる場合と、
想定外の例外（XH28）を起こす場合だけ。
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from pathlib import Path

import pytest
from mcp import Client
from mcp.types import CallToolResult
from typer.testing import CliRunner

from caucus_race.cli import main as cli
from caucus_race.domain.entities import Feedback, Spike, Story
from caucus_race.domain.errors import DomainError, InvalidTransitionError, NotFoundError
from caucus_race.domain.status import SpikeStatus, SprintStatus, StoryStatus
from caucus_race.mcp.server import create_server
from caucus_race.port.workspace import WorkspaceError
from caucus_race.runner import BackgroundRunner, RunLock, spawn_detached
from caucus_race.usecase.project import ProjectService
from tests.conftest import git
from tests.test_mcp_human_tools import Env, FakeSpawn, P
from tests.test_mcp_human_tools_combinations import lock_state, put_sprint_in, sprint_state

NO_BASE = "no-base"


@pytest.fixture
def env(tmp_path: Path, git_repo: Path) -> Env:
    e = Env(tmp_path, git_repo)
    e.injector.get(ProjectService).register(NO_BASE, git_repo, base_branch="no-such-branch")
    return e


def branches(repo: Path) -> set[str]:
    return set(git(repo, "branch", "--format=%(refname:short)").split())


def story_statuses(env: Env) -> list[tuple[str, str, str | None]]:
    return sorted((s.id, s.status, s.sprint_id) for s in env.repo.query(Story))


def use_real_spawn_with_unwritable_log_dir(env: Env) -> None:
    """本物のプロセスの起動を使い、
    ログのディレクトリの場所にファイルを置いて作れなくする（ファイル境界の失敗）。"""
    env.injector.get(BackgroundRunner).spawn = spawn_detached
    env.settings.log_dir.parent.mkdir(parents=True, exist_ok=True)
    env.settings.log_dir.write_text("ディレクトリではない", encoding="utf-8")


# --- スプリントの開始（start_sprint） ---


def test_XH1_start_sprint_while_running_raises_already_running_and_changes_nothing(
    env: Env, git_repo: Path
) -> None:
    """P が実行中のとき、start_sprint は DomainError「既に実行中」（pid を含む）になり、
    スプリントは承認済みのまま、ストーリーも変わらず、ブランチも作られず、プロセスも起動しないこと。"""
    put_sprint_in(env, "approved")
    before = (sprint_state(env), story_statuses(env), branches(git_repo))

    with (
        lock_state(env, "running"),
        pytest.raises(DomainError, match=r"既に実行中です（pid \d+）"),
    ):
        env.tools.start_sprint(P)

    assert (sprint_state(env), story_statuses(env), branches(git_repo)) == before
    assert env.spawn.calls == []


@pytest.mark.parametrize(
    ("setup", "error", "message"),
    [
        pytest.param(
            "planning", InvalidTransitionError, r"承認されていません（planning）", id="XH2-not-approved"
        ),
        pytest.param(
            "open-spike", DomainError, r"未解決のスパイクがあるため開始できません: SP-1", id="XH3-open-spike"
        ),
        pytest.param("dirty", DomainError, r"未コミットの変更があるため開始できません", id="XH4-dirty-repo"),
        pytest.param(
            "branch-exists", DomainError, r"ブランチ caucus/sprint-1 が既に存在します", id="XH5-branch-exists"
        ),
    ],
)
def test_XH2_5_start_sprint_rejected_by_start_conditions_changes_nothing(
    env: Env, git_repo: Path, setup: str, error: type[DomainError], message: str
) -> None:
    """開始の条件（R-2: 承認済み・未解決のスパイクなし。作業ツリーがきれい・ブランチが未作成）を
    満たさないとき、
    理由を表すメッセージの例外になり、スプリント・ストーリー・ブランチは変わらず、
    プロセスも起動しないこと。"""
    story = put_sprint_in(env, "planning")
    assert story is not None
    if setup == "open-spike":
        env.planning.add_spike(story.id, "並び順は？")
    if setup != "planning":
        env.planning.approve(P)
    if setup == "dirty":
        (git_repo / "untracked.txt").write_text("未コミット", encoding="utf-8")
    if setup == "branch-exists":
        git(git_repo, "branch", "caucus/sprint-1")
    before = (sprint_state(env), story_statuses(env), branches(git_repo))

    with pytest.raises(error, match=message):
        env.tools.start_sprint(P)

    assert (sprint_state(env), story_statuses(env), branches(git_repo)) == before
    assert env.spawn.calls == []


def test_XH6_start_sprint_git_failure_raises_workspace_error_and_rolls_back(env: Env, git_repo: Path) -> None:
    """ブランチの作成に git が失敗したとき（ベースブランチが存在しない）、
    WorkspaceError「git ... に失敗しました」に
    なり、スプリントは承認済みのまま、ストーリーも queued にならず（途中の変更が残らない）、
    プロセスも起動しないこと。"""
    put_sprint_in(env, "approved", project=NO_BASE)
    before = (sprint_state(env, NO_BASE), story_statuses(env), branches(git_repo))

    with pytest.raises(WorkspaceError, match=r"git .*に失敗しました"):
        env.tools.start_sprint(NO_BASE)

    assert (sprint_state(env, NO_BASE), story_statuses(env), branches(git_repo)) == before
    assert env.spawn.calls == []


def test_XH7_start_sprint_launch_failure_raises_domain_error_with_original_cause(env: Env) -> None:
    """実行プロセスを起動できないとき（ログのディレクトリを作れない）、OSError ではなく
    DomainError「プロセスを起動できませんでした」（元の理由を含む）になり、元の OSError が原因として残ること。
    起動に失敗した後のスプリントの状態（active とブランチを残すか、承認済みに戻すか）は
    仕様不明のため確かめない。"""
    put_sprint_in(env, "approved")
    use_real_spawn_with_unwritable_log_dir(env)

    with pytest.raises(DomainError, match=r"プロセスを起動できませんでした: .*logs") as excinfo:
        env.tools.start_sprint(P)

    assert isinstance(excinfo.value.__cause__, OSError)


# --- 再開・停止（resume_sprint / stop_sprint） ---


def test_XH8_resume_sprint_while_running_raises_already_running(env: Env) -> None:
    """P が実行中のとき、resume_sprint は DomainError「既に実行中」になり、プロセスを起動せず、
    実行中のプロセスのロックはそのまま残ること。"""
    put_sprint_in(env, "active")

    with lock_state(env, "running"):
        with pytest.raises(DomainError, match=r"既に実行中です（pid \d+）"):
            env.tools.resume_sprint(P)
        assert env.injector.get(BackgroundRunner).running(P) is not None

    assert env.spawn.calls == []
    assert sprint_state(env)[0] == SprintStatus.ACTIVE


def test_XH9_resume_sprint_launch_failure_raises_domain_error_and_keeps_sprint_active(env: Env) -> None:
    """停止中のスプリントの実行プロセスを起動できないとき、
    DomainError「プロセスを起動できませんでした」になり、
    スプリントは active、ストーリーは queued のまま（停止中のまま。もう一度再開できる状態）であること。"""
    story = put_sprint_in(env, "active")
    assert story is not None
    use_real_spawn_with_unwritable_log_dir(env)

    with pytest.raises(DomainError, match=r"プロセスを起動できませんでした"):
        env.tools.resume_sprint(P)

    assert sprint_state(env)[0] == SprintStatus.ACTIVE
    assert env.repo.get(Story, story.id).status == StoryStatus.QUEUED


@pytest.mark.parametrize(
    ("lock", "project"),
    [
        pytest.param("idle", P, id="XH10-no-lock-file"),
        pytest.param("stale", P, id="XH11-stale-lock-file"),
        pytest.param("idle", "nope", id="XH12-missing-project"),
    ],
)
def test_XH10_12_stop_sprint_when_not_running_raises_not_running(env: Env, lock: str, project: str) -> None:
    """P が実行中でないとき（ロックファイルがない・前回のファイルだけ残る・プロジェクトが存在しない）、
    stop_sprint は DomainError「<project> のスプリントは実行されていません」になり、
    スプリントは変わらないこと。"""
    put_sprint_in(env, "active")
    before = sprint_state(env)

    with (
        lock_state(env, lock),
        pytest.raises(DomainError, match=f"{project} のスプリントは実行されていません"),
    ):
        env.tools.stop_sprint(project)

    assert sprint_state(env) == before


def test_XH13_stop_sprint_with_unreadable_pid_raises_and_keeps_lock(env: Env) -> None:
    """ロックは持たれているが pid が読めないとき、
    stop_sprint は DomainError「実行中のプロセスを特定できません」になり、
    どのプロセスにも停止を送らない
    （ロックを持つ側はロックを持ったまま。pid 0 に送るとこのテスト自身が止まる）こと。"""
    put_sprint_in(env, "active")

    with lock_state(env, "running-pid-unknown"):
        with pytest.raises(DomainError, match=r"実行中のプロセスを特定できません"):
            env.tools.stop_sprint(P)
        assert env.injector.get(BackgroundRunner).running(P) is not None


# --- 計画の承認・差し戻し ---


@pytest.mark.parametrize(
    "comment", [pytest.param("", id="XH14-empty"), pytest.param(" 　\n", id="XH15-blank")]
)
def test_XH14_15_reject_plan_without_comment_raises_comment_required(env: Env, comment: str) -> None:
    """コメントが空・空白だけのとき、reject_plan は DomainError「差し戻しにはコメントが必要です」になり、
    スプリントは承認済みのまま、差し戻しコメントも残らないこと。"""
    put_sprint_in(env, "approved")

    with pytest.raises(DomainError, match=r"コメントが必要です"):
        env.tools.reject_plan(P, comment)

    assert sprint_state(env) == (SprintStatus.APPROVED, None)


def test_XH16_reject_plan_of_active_sprint_raises_invalid_transition(env: Env) -> None:
    """実行中（active）のスプリントを差し戻そうとしたとき、InvalidTransitionError「active です」になり、
    スプリントは active のまま、差し戻しコメントも残らないこと。"""
    put_sprint_in(env, "active")

    with pytest.raises(InvalidTransitionError, match=r"スプリント #1 は active です"):
        env.tools.reject_plan(P, "やり直したい")

    assert sprint_state(env) == (SprintStatus.ACTIVE, None)


@pytest.mark.parametrize(
    ("state", "error", "message"),
    [
        pytest.param("planning-empty", DomainError, r"ストーリーが 1 件もない", id="XH17-no-story"),
        pytest.param(
            "approved", InvalidTransitionError, r"approved のため計画を変更できません", id="XH18-already"
        ),
        pytest.param("none", DomainError, r"demo に進行中のスプリントがありません", id="XH19-no-sprint"),
    ],
)
def test_XH17_19_approve_plan_rejected_keeps_state(
    env: Env, state: str, error: type[DomainError], message: str
) -> None:
    """承認できない状態（ストーリーが 0 件・既に承認済み・スプリントがない）のとき、
    理由を表すメッセージの例外になり、
    スプリントの状態は変わらないこと。"""
    if state == "planning-empty":
        env.planning.new_sprint(P, "空のスプリント")
    else:
        put_sprint_in(env, state)
    before = sprint_state(env)

    with pytest.raises(error, match=message):
        env.tools.approve_plan(P)

    assert sprint_state(env) == before


# --- スパイク ---


def test_XH20_answer_unknown_spike_raises_not_found_without_launching(env: Env) -> None:
    """存在しないスパイクに回答したとき、NotFoundError「SP-99 が見つかりません」になり、
    見直しも起動しないこと。"""
    put_sprint_in(env, "planning")

    with pytest.raises(NotFoundError, match=r"SP-99 が見つかりません"):
        env.tools.answer_spike("SP-99", "新しい順")

    assert env.spawn.calls == []


def test_XH21_answer_already_answered_spike_raises_and_keeps_first_answer(env: Env) -> None:
    """回答済みのスパイクにもう一度回答したとき、
    InvalidTransitionError「回答待ちではありません（answered）」になり、
    最初の回答と決定事項は変わらず、見直しも起動しないこと。"""
    story = put_sprint_in(env, "planning")
    assert story is not None
    spike = env.planning.add_spike(story.id, "並び順は？")
    env.planning.answer_spike(spike.id, "古い順")
    before = env.repo.get(Story, story.id).decisions

    with pytest.raises(InvalidTransitionError, match=r"回答待ちではありません（answered）"):
        env.tools.answer_spike(spike.id, "新しい順")

    after = env.repo.get(Spike, spike.id)
    assert (after.status, after.answer) == (SpikeStatus.ANSWERED, "古い順")
    assert env.repo.get(Story, story.id).decisions == before
    assert env.spawn.calls == []


def test_XH33_answer_spike_refine_launch_failure_raises_domain_error_with_original_cause(env: Env) -> None:
    """AI プランナーが設定済みで最後のスパイクに回答し、見直しのプロセスを起動できないとき
    （ログのディレクトリを作れない）、OSError ではなく
    DomainError「プロセスを起動できませんでした」（元の理由を含む）になり、
    元の OSError が原因として残ること（受け入れ条件: ドメインのエラーは日本語のツールエラーとして返る）。
    回答を記録したまま残すか取り消すかは仕様不明のため確かめない。"""
    story = put_sprint_in(env, "planning")
    assert story is not None
    spike = env.planning.add_spike(story.id, "並び順は？")
    use_real_spawn_with_unwritable_log_dir(env)

    with pytest.raises(DomainError, match=r"プロセスを起動できませんでした: .*logs") as excinfo:
        env.tools.answer_spike(spike.id, "新しい順")

    assert isinstance(excinfo.value.__cause__, OSError)


def test_XH22_list_spikes_of_unknown_project_raises_not_found(env: Env) -> None:
    """存在しないプロジェクトのスパイクを一覧しようとしたとき、
    NotFoundError「プロジェクト nope が見つかりません」になること。"""
    with pytest.raises(NotFoundError, match=r"プロジェクト nope が見つかりません"):
        env.tools.list_spikes("nope")


# --- レビュー ---


def test_XH23_get_review_outside_review_raises_and_writes_no_report(env: Env) -> None:
    """スプリントがレビュー待ちでないとき、
    get_review は InvalidTransitionError「レビュー待ちではありません（active）」に
    なり、レビュー資料のファイルを作らないこと。"""
    put_sprint_in(env, "active")
    reviews = env.settings.data_dir / "reviews"

    with pytest.raises(InvalidTransitionError, match=r"レビュー待ちではありません（active）"):
        env.tools.get_review(P)

    assert not reviews.exists() or list(reviews.rglob("*.md")) == []


@pytest.mark.parametrize(
    ("state", "story", "error", "message"),
    [
        pytest.param(
            "active",
            None,
            InvalidTransitionError,
            r"レビュー待ちではありません（active）",
            id="XH24-not-review",
        ),
        pytest.param(
            "review", "backlog", DomainError, r"はスプリント #1 のストーリーではありません", id="XH25-outside"
        ),
        pytest.param("review", "ST-999", NotFoundError, r"ST-999 が見つかりません", id="XH26-missing-story"),
    ],
)
def test_XH24_26_add_feedback_rejected_adds_nothing(
    env: Env, state: str, story: str | None, error: type[DomainError], message: str
) -> None:
    """レビュー待ちでない、またはスプリント外・存在しないストーリーを指定したとき、
    理由を表すメッセージの例外になり、
    フィードバックは 1 件も登録されないこと（途中まで登録されない）。"""
    put_sprint_in(env, state)
    story_id = env.backlog.add_story(P, "バックログ").id if story == "backlog" else story

    with pytest.raises(error, match=message):
        env.tools.add_feedback(P, "件数も表示したい", story_id)

    assert env.repo.query(Feedback) == []


def test_XH27_complete_review_outside_review_raises_and_changes_nothing(env: Env) -> None:
    """スプリントがレビュー待ちでないとき、
    complete_review は InvalidTransitionError「レビュー待ちではありません」になり、
    スプリントの状態・ストーリー・バックログは変わらないこと。"""
    put_sprint_in(env, "active")
    before = (sprint_state(env), story_statuses(env))

    with pytest.raises(InvalidTransitionError, match=r"レビュー待ちではありません（active）"):
        env.tools.complete_review(P)

    assert (sprint_state(env), story_statuses(env)) == before


# --- 想定外の例外 ---


class BrokenSpawn(FakeSpawn):
    def __call__(self, argv: list[str], log_path: Path) -> int:
        raise RuntimeError("起動器の不具合")


def test_XH28_unexpected_error_is_not_disguised_as_domain_error(env: Env) -> None:
    """プロセスの起動で想定外の例外（OSError でも DomainError でもない）が起きたとき、DomainError に包まず
    そのままの型で伝わること（不具合をルール違反に見せかけない）。
    スプリントは active、ストーリーは queued のまま（停止中のまま）であること。"""
    story = put_sprint_in(env, "active")
    assert story is not None
    env.injector.get(BackgroundRunner).spawn = BrokenSpawn()

    with pytest.raises(RuntimeError, match=r"起動器の不具合"):
        env.tools.resume_sprint(P)

    assert sprint_state(env)[0] == SprintStatus.ACTIVE
    assert env.repo.get(Story, story.id).status == StoryStatus.QUEUED


# --- MCP の層 ---


def with_client[T](env: Env, body: Callable[[Client], Awaitable[T]]) -> T:
    async def run() -> T:
        async with Client(create_server(env.injector)) as client:
            return await body(client)

    return asyncio.run(run())


def text(result: CallToolResult) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


@pytest.mark.parametrize(
    ("setup", "name", "arguments", "message"),
    [
        pytest.param(
            "running",
            "start_sprint",
            {"project": P},
            r"^このプロジェクトのスプリントは既に実行中です（pid \d+）$",
            id="start-running",
        ),
        pytest.param(
            "active",
            "stop_sprint",
            {"project": P},
            rf"^{P} のスプリントは実行されていません$",
            id="stop-not-running",
        ),
        pytest.param(
            "approved",
            "reject_plan",
            {"project": P, "comment": " "},
            r"^差し戻しにはコメントが必要です$",
            id="reject-blank",
        ),
        pytest.param(
            "no-base", "start_sprint", {"project": NO_BASE}, r"^git .*に失敗しました", id="workspace-error"
        ),
        pytest.param(
            "active",
            "add_feedback",
            {"project": P, "body": "x"},
            r"^スプリント #1 はレビュー待ちではありません（active）$",
            id="feedback-not-review",
        ),
    ],
)
def test_XH29_rule_violation_reaches_agent_as_tool_error_with_japanese_message(
    env: Env, setup: str, name: str, arguments: dict, message: str
) -> None:
    """MCP のツールとして呼んでルール違反（DomainError・WorkspaceError）になったとき、
    トレースバックや SDK の前置き
    （Error executing tool）ではなく、日本語の理由だけを本文にした isError の結果になること。"""
    if setup == "no-base":
        put_sprint_in(env, "approved", project=NO_BASE)
    elif setup == "running":
        put_sprint_in(env, "approved")
    else:
        put_sprint_in(env, setup)

    with lock_state(env, "running" if setup == "running" else "idle"):
        result = with_client(env, lambda c: c.call_tool(name, arguments))

    assert result.is_error is True
    assert re.search(message, text(result)), text(result)
    assert "Traceback" not in text(result)


def test_XH30_unexpected_error_becomes_tool_error_and_server_keeps_serving(env: Env) -> None:
    """MCP のツールとして呼んで想定外の例外が起きたとき、isError の結果が返り、サーバーは止まらず、
    続けて呼んだツールは動くこと。"""
    put_sprint_in(env, "active")
    env.injector.get(BackgroundRunner).spawn = BrokenSpawn()

    async def body(c: Client) -> tuple[CallToolResult, CallToolResult]:
        failed = await c.call_tool("resume_sprint", {"project": P})
        listed = await c.call_tool("list_spikes", {"project": P})
        return failed, listed

    failed, listed = with_client(env, body)

    assert failed.is_error is True
    assert listed.is_error is not True


# --- CLI（切り出した処理の例外が、CLI に以前と同じ形で伝わること） ---

runner = CliRunner()


@pytest.fixture
def cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_repo: Path) -> Path:
    home = tmp_path / "cli-home"
    monkeypatch.setenv("CAUCUS_HOME", str(home))
    cli.container.cache_clear()
    assert runner.invoke(cli.app, ["project", "add", P, "--repo", str(git_repo)]).exit_code == 0
    return home


def test_XH31_cli_sprint_stop_when_not_running_raises_same_error(cli_home: Path) -> None:
    """`caucus sprint stop` を実行中でないときに呼ぶと、切り出し前と同じ DomainError
    「<project> のスプリントは実行されていません」で終わること（CLI の振る舞いを変えない）。"""
    result = runner.invoke(cli.app, ["sprint", "stop", P])

    assert isinstance(result.exception, DomainError)
    assert str(result.exception) == f"{P} のスプリントは実行されていません"


def test_XH32_cli_sprint_resume_detach_while_running_raises_same_error(cli_home: Path) -> None:
    """`caucus sprint resume --detach` を実行中に呼ぶと、
    切り出し前と同じ DomainError「既に実行中です（pid N）」で
    終わり、何も出力しないこと（CLI の振る舞いを変えない）。"""
    with RunLock(cli_home / "run", P).hold():
        result = runner.invoke(cli.app, ["sprint", "resume", P, "--detach"])

    assert isinstance(result.exception, DomainError)
    assert re.fullmatch(r"このプロジェクトのスプリントは既に実行中です（pid \d+）", str(result.exception))
    assert result.output == ""


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        pytest.param(["sprint", "stop", P], rf"エラー: {P} のスプリントは実行されていません", id="XH34-stop"),
        pytest.param(
            ["sprint", "resume", P, "--detach"],
            r"エラー: このプロジェクトのスプリントは既に実行中です（pid \d+）",
            id="XH35-resume-detach",
        ),
    ],
)
def test_XH34_35_cli_main_prints_error_and_exits_1(
    cli_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    message: str,
) -> None:
    """`caucus`（`main()`）として呼んで切り出した処理が DomainError になったとき、トレースバックではなく
    標準エラーに「エラー: <理由>」を表示し、終了コード 1 で終わること（切り出し前の CLI と同じ）。"""
    monkeypatch.setattr("sys.argv", ["caucus", *argv])
    capsys.readouterr()

    # resume は実行中であることが条件なのでロックを持つ。stop は実行中でないことが条件なので持たない
    lock = RunLock(cli_home / "run", P).hold() if argv[1] == "resume" else nullcontext()
    with lock, pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert re.search(message, err), err
    assert "Traceback" not in err
