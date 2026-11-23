"""MCP の起票・スプリント計画ツールと計画ジョブのプロパティテスト。

設計表: docs/test-design/20261123-caucus-sprint-1.md の「## プロパティテスト」。

Hypothesis は開発用の依存に未追加（`uv add --dev hypothesis` が作業環境の権限で拒否された）。
追加されるまでは、このファイル全体が理由付きで skip される（`pytest -rs` で表示される）。
生成した入力ではまだ一度も実行していない。

性質を確かめる本体は `check_*` 関数にし、`@given` はそれに入力を渡すだけにしている。

入力の文字列について:
- file_stem（P1・P2）は任意の `str` を受け付ける純粋な関数なので、サロゲートも含めて生成する
- DB や状態ファイルを通る性質（P3〜P8）では、サロゲートを生成しない。既存の永続化（pydantic の JSON・
  SQLite）がサロゲートを保存できず、サロゲートを含むプロジェクトやストーリーは作れないため。
  入口で分かりやすく拒否するかどうかは仕様不明で、別途報告している
"""

import os
import tempfile
from pathlib import Path

import pytest

hypothesis = pytest.importorskip(
    "hypothesis", reason="Hypothesis が未導入のため skip（uv add --dev hypothesis で追加すると動く）"
)

from hypothesis import HealthCheck, example, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from caucus_race.domain.entities import Project, Spike, Sprint, Story, Task  # noqa: E402
from caucus_race.domain.errors import DomainError, InvalidTransitionError, NotFoundError  # noqa: E402
from caucus_race.mcp.planning_tools import ToolFailure, call_tool  # noqa: E402
from caucus_race.plan_job import PlanJobs, file_stem  # noqa: E402
from caucus_race.port.planner import PlannerError  # noqa: E402
from caucus_race.port.workspace import WorkspaceError  # noqa: E402
from tests.test_mcp_planning import Env, GatedPlanner, P  # noqa: E402

# ファイル名の長さの上限（多くのファイルシステムで 255 バイト）
NAME_MAX = 255
# 計画ジョブが作るファイルのうち、最も長い接尾辞
LONGEST_SUFFIX = "-plan-20261123-120000.log"

# --- 文字列の生成 ---

# 非 ASCII（エンコードで長くなる）だけの、長さ 0〜100 文字の文字列。切り詰めの分岐を通す
long_non_ascii = st.text(st.characters(min_codepoint=0x3000, max_codepoint=0x9FFF), max_size=100)
# サロゲートを含まない任意の文字列。通常の文字列と、長い非 ASCII の文字列を混ぜる
plain_text = st.one_of(st.text(), long_non_ascii)
# file_stem 用: 通常の文字とサロゲートが混ざった文字列も含む、型が受け付ける任意の文字列
any_text = st.one_of(
    plain_text,
    st.text(st.one_of(st.characters(), st.characters(categories=["Cs"])), min_size=1),
)
# 1 文字以上の文字列（リストの要素が空文字の場合の扱いは仕様不明のため除く）
nonempty_text = st.text(min_size=1)

LONG_NAME = "記" * 40  # エンコードで 360 バイトになり、切り詰めが必要な名前
# エンコード後の長さが切り詰めの境目の前後になる名前（ASCII はエンコードで長さが変わらない）
EDGE_NAMES = ["a" * 199, "a" * 200, "a" * 201]

# DB を使うテストは 1 件ごとにデータディレクトリを作るため遅い。件数を抑える
DB_SETTINGS = settings(
    max_examples=30,
    deadline=None,
    # git_repo は登録に使うだけで変更しないため、例ごとに作り直さなくてよい
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


def make_env(git_repo: Path, limit: int = 5) -> Env:
    env = Env(Path(tempfile.mkdtemp()), GatedPlanner())
    env.projects.register(P, git_repo, max_stories_per_sprint=limit, planner_config={"type": "claude"})
    return env


# --- P1・P2: file_stem ---


def check_file_stem_is_one_safe_file(name: str, directory: Path) -> None:
    stem = file_stem(name)
    assert stem.isascii(), stem
    assert "/" not in stem and "\0" not in stem
    assert stem not in (".", "..")  # 空の名前は接尾辞が付くので 1 つのファイルになる
    filename = f"{stem}{LONGEST_SUFFIX}"
    assert len(filename.encode()) <= NAME_MAX, len(filename.encode())
    path = directory / filename
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    os.close(fd)
    assert path.resolve().parent == directory.resolve()


@given(name=any_text)
@example(name="")
@example(name=".")
@example(name="..")
@example(name="../escape")
@example(name="team/app")
@example(name=LONG_NAME)
@example(name="\ud800")
@example(name="記録\ud800" + "記" * 30)
@example(name=EDGE_NAMES[0])
@example(name=EDGE_NAMES[1])
@example(name=EDGE_NAMES[2])
@settings(deadline=None)
def test_property_file_stem_is_always_one_safe_file(name: str) -> None:
    """P1: どんなプロジェクト名でも、計画ジョブのファイル名は、指定したディレクトリの直下に作れる
    1 つのファイル名（ASCII、`/` なし、最も長い接尾辞を付けても上限の 255 バイト以下）になること。"""
    with tempfile.TemporaryDirectory() as d:
        check_file_stem_is_one_safe_file(name, Path(d))


def check_file_stems_are_distinct(names: list[str]) -> None:
    stems = [file_stem(n) for n in names]
    assert len(set(stems)) == len(names), list(zip(names, stems, strict=True))


@given(names=st.lists(any_text, min_size=2, max_size=5, unique=True))
@example(names=["a/b", "a%2Fb"])
@example(names=[".", "%2E", "..", "%2E%2E"])
@example(names=[LONG_NAME, LONG_NAME + "x", LONG_NAME[:-1]])
@example(names=["\ud800", "%ED%A0%80"])
@example(names=EDGE_NAMES)
@example(names=["Foo", "foo", "FOO"])  # 大文字小文字を区別しないファイルシステムで重なる組
def test_property_file_stem_is_injective(names: list[str]) -> None:
    """P2: 異なるプロジェクト名は、異なるファイル名になること（別のプロジェクトとロックを共有しない）。"""
    check_file_stems_are_distinct(names)


# --- P3: 計画ジョブの結果の往復 ---


class HoldingLauncher:
    """ジョブのプロセスの代わり（プロセス境界）。ロックを持った記述子を持ち続け、close で手放す。"""

    def __init__(self) -> None:
        self.held: list[int] = []

    def __call__(self, argv: list[str], log_path: Path, lock_fd: int) -> None:
        self.held.append(os.dup(lock_fd))

    def close(self) -> None:
        while self.held:
            os.close(self.held.pop())


def check_plan_job_round_trip(
    project: str, number: int, goal: str, story_ids: list[str], error: str | None
) -> None:
    launcher = HoldingLauncher()
    with tempfile.TemporaryDirectory() as d:
        jobs = PlanJobs(Path(d) / "run", Path(d) / "logs", launcher)
        jobs.start(project)
        sprint = None if error else Sprint(id="SPR-1", project_id="PRJ-1", number=number, goal=goal)
        jobs.finish(project, sprint=sprint, story_ids=[] if error else story_ids, error=error)
        launcher.close()  # プロセスの終了

        state = jobs.status(project)
        assert state is not None
        if error:
            assert (state.status, state.error) == ("failed", error)
        else:
            assert (state.status, state.sprint_number, state.goal, state.story_ids, state.error) == (
                "succeeded",
                number,
                goal,
                story_ids,
                None,
            )


@given(
    project=plain_text,
    number=st.integers(min_value=1, max_value=10**9),
    goal=plain_text,
    story_ids=st.lists(st.text()),
    # 空文字の理由は「既定の文言で failed」になるが、それが仕様どおりかは決まっていないため除く
    error=st.one_of(st.none(), nonempty_text),
)
@example(project=LONG_NAME, number=1, goal="ゴール\n2 行目", story_ids=["ST-1"], error=None)
@example(project="team/app", number=1, goal="", story_ids=[], error="理由\n2 行目")
@example(project="", number=1, goal="", story_ids=[], error=None)
@example(project=EDGE_NAMES[1], number=1, goal="x", story_ids=[], error=None)
@example(project=EDGE_NAMES[2], number=1, goal="x", story_ids=[], error=None)
@settings(deadline=None)
def test_property_plan_job_result_round_trips(
    project: str, number: int, goal: str, story_ids: list[str], error: str | None
) -> None:
    """P3: 計画ジョブに書いた結果（成功ならスプリント番号・ゴール・ストーリー、失敗なら理由）が、
    どんなプロジェクト名・値でも、問い合わせでそのまま読めること。"""
    check_plan_job_round_trip(project, number, goal, story_ids, error)


# --- P8: 状態ファイルが壊れていても、結果を書き直す ---


def check_finish_overwrites_any_broken_state(project: str, garbage: bytes, goal: str) -> None:
    launcher = HoldingLauncher()
    with tempfile.TemporaryDirectory() as d:
        run_dir = Path(d) / "run"
        jobs = PlanJobs(run_dir, Path(d) / "logs", launcher)
        jobs.start(project)
        for path in run_dir.glob("*.plan.json"):  # ファイル境界: 状態ファイルの中身を任意のバイト列にする
            path.write_bytes(garbage)
        sprint = Sprint(id="SPR-1", project_id="PRJ-1", number=1, goal=goal)
        jobs.finish(project, sprint=sprint, story_ids=["ST-1"], error=None)
        launcher.close()

        state = jobs.status(project)
        assert state is not None
        assert (state.status, state.goal, state.story_ids) == ("succeeded", goal, ["ST-1"])


@given(project=plain_text, garbage=st.binary(), goal=plain_text)
@example(project="demo", garbage=b"", goal="ゴール")
@example(project="demo", garbage=b"{\x80\xff", goal="ゴール")
@example(project="demo", garbage=b'{"status": "running"}', goal="ゴール")  # JSON として正しいが項目が足りない
@settings(deadline=None)
def test_property_finish_overwrites_any_broken_state_file(project: str, garbage: bytes, goal: str) -> None:
    """P8: 計画の処理の途中で状態ファイルがどんなバイト列になっていても、結果を書き終えた後の問い合わせは
    書いた結果を返すこと（DB に保存した計画と食い違わない）。"""
    check_finish_overwrites_any_broken_state(project, garbage, goal)


# --- P4・P5: 起票の往復とタスクの順序 ---


def check_story_round_trip(env: Env, title: str, description: str, criteria: list[str]) -> None:
    result = env.tools.add_story(P, title, description, criteria)
    stored = env.repo.get(Story, result.id)
    assert (stored.title, stored.description, stored.acceptance_criteria) == (title, description, criteria)
    assert (result.title, result.description, result.acceptance_criteria) == (title, description, criteria)


@given(title=plain_text, description=plain_text, criteria=st.lists(nonempty_text, max_size=5))
@example(title="一覧画面", description="1 行目\n2 行目", criteria=["新しい順\nに並ぶ", "記" * 1000])
@example(title="", description="", criteria=[])
@DB_SETTINGS
def test_property_story_fields_round_trip(
    git_repo: Path, title: str, description: str, criteria: list[str]
) -> None:
    """P4: 起票したストーリーのタイトル・説明・受け入れ条件が、
    どんな文字列でもそのまま保存されて読めること。"""
    check_story_round_trip(make_env(git_repo), title, description, criteria)


def check_task_round_trip(env: Env, title: str, description: str) -> None:
    story = env.tools.add_story(P, "対象")
    result = env.tools.add_task(story.id, title, description)
    stored = env.repo.query(Task, story_id=story.id)
    assert [(t.title, t.description) for t in stored] == [(title, description)]
    assert (result.story_id, result.title, result.description) == (story.id, title, description)


@given(title=plain_text, description=plain_text)
@example(title="一覧 API", description="一覧を返す。\n件数も返す")
@example(title="", description="")
@DB_SETTINGS
def test_property_task_fields_round_trip(git_repo: Path, title: str, description: str) -> None:
    """P4: 追加したタスクのタイトル・説明が、どんな文字列でもそのまま保存されて読めること。"""
    check_task_round_trip(make_env(git_repo), title, description)


def check_spike_round_trip(
    env: Env, question: str, context: str, options: list[str], recommendation: str | None
) -> None:
    story = env.tools.add_story(P, "対象")
    result = env.tools.add_spike(story.id, question, context, options, recommendation)
    saved = env.repo.get(Spike, result.id)
    expected = (question, context, options, recommendation)
    assert (saved.question, saved.context, saved.options, saved.recommendation) == expected
    assert (result.question, result.context, result.options, result.recommendation) == expected


@given(
    question=plain_text,
    context=plain_text,
    options=st.lists(nonempty_text, max_size=5),
    recommendation=st.one_of(st.none(), plain_text),
)
@example(
    question="ページングは必要か", context="背景\n2 行目", options=["必要", "不要\n後で"], recommendation=None
)
@DB_SETTINGS
def test_property_spike_fields_round_trip(
    git_repo: Path, question: str, context: str, options: list[str], recommendation: str | None
) -> None:
    """P4: 起票したスパイクの質問・背景・選択肢・推奨が、どんな文字列でもそのまま保存されて読めること。"""
    check_spike_round_trip(make_env(git_repo), question, context, options, recommendation)


def check_tasks_keep_added_order(env: Env, titles: list[str]) -> None:
    story = env.tools.add_story(P, "対象")
    env.tools.create_sprint(P, "ゴール")
    for title in titles:
        env.tools.add_task(story.id, title)
    result = env.tools.add_story_to_sprint(P, story.id)
    assert result.stories[0].tasks == titles  # モデル: 追加した順のリスト
    assert len(env.repo.query(Task, story_id=story.id)) == len(titles)


@given(titles=st.lists(plain_text, max_size=8))
@example(titles=["同じ", "同じ", "別"])
@DB_SETTINGS
def test_property_tasks_keep_added_order(git_repo: Path, titles: list[str]) -> None:
    """P5: スプリントの内容のタスクは、どんな件数・内容（重複を含む）でも、
    追加した順のリストと一致すること。"""
    check_tasks_keep_added_order(make_env(git_repo), titles)


# --- P6: スプリントの中身と、集合のモデルとの一致 ---

# 操作: (種類, 対象)。対象は自分のストーリーの番号、"missing"（存在しない ID）、
# "other"（別のプロジェクトのストーリー）
targets = st.one_of(st.integers(0, 4), st.sampled_from(["missing", "other"]))
operations = st.lists(
    st.one_of(st.tuples(st.sampled_from(["add", "remove"]), targets), st.just(("approve", 0))), max_size=15
)


def check_sprint_matches_set_model(
    env: Env, git_repo: Path, limit: int, n_stories: int, ops: list[tuple[str, int | str]]
) -> None:
    env.projects.register("other", git_repo)
    other_story = env.tools.add_story("other", "別のプロジェクト").id
    stories = [env.tools.add_story(P, f"候補 {i}").id for i in range(n_stories)]
    env.tools.create_sprint(P, "ゴール")
    project_id = env.repo.query(Project, name=P)[0].id
    # モデル: スプリントに入っている自分のストーリーの集合と、承認済みかどうか
    members: set[str] = set()
    approved = False

    for kind, target in ops:
        if kind == "approve":
            allowed = not approved and bool(members)
            # 承認は人間が CLI で行う
            call = lambda: env.planning.approve(P)  # noqa: E731
        else:
            if target == "missing":
                story_id = "ST-999999"
            elif target == "other":
                story_id = other_story
            else:
                story_id = stories[int(target) % n_stories]
            own = story_id in stories
            if kind == "add":
                allowed = own and not approved and story_id not in members and len(members) < limit
                call = lambda s=story_id: env.tools.add_story_to_sprint(P, s)  # noqa: E731
            else:
                allowed = own and not approved and story_id in members
                call = lambda s=story_id: env.tools.remove_story_from_sprint(P, s)  # noqa: E731

        if allowed:
            call()
            if kind == "approve":
                approved = True
            elif kind == "add":
                members = members | {story_id}
            else:
                members = members - {story_id}
        else:
            with pytest.raises(DomainError):
                call()

        sprint = env.repo.query(Sprint, project_id=project_id)[0]
        actual = {s.id for s in env.repo.query(Story, sprint_id=sprint.id)}
        assert actual == members, (kind, target)
        assert len(actual) <= limit
        assert (sprint.status == "approved") == approved, (kind, target)


@given(limit=st.integers(1, 3), n_stories=st.integers(1, 5), ops=operations)
@example(limit=1, n_stories=2, ops=[("add", 0), ("add", 1), ("remove", 0), ("add", 1), ("add", 0)])
@example(limit=2, n_stories=2, ops=[("add", "other"), ("add", "missing"), ("remove", "other"), ("add", 0)])
@example(limit=2, n_stories=2, ops=[("approve", 0), ("add", 0), ("approve", 0), ("add", 1), ("remove", 0)])
@DB_SETTINGS
def test_property_sprint_membership_matches_set_model(
    git_repo: Path, limit: int, n_stories: int, ops: list[tuple[str, int | str]]
) -> None:
    """P6: 追加・除外・承認のどんな操作列でも、スプリントの中身・承認の状態・各操作の成否が、
    「集合・上限・承認済みかどうか」だけの単純なモデルと一致し、件数が上限を超えないこと。
    存在しない ID や別のプロジェクトのストーリーは、常に拒否されて何も変えないこと。"""
    check_sprint_matches_set_model(make_env(git_repo, limit), git_repo, limit, n_stories, ops)


# --- P7: call_tool の例外の性質 ---

USER_ERROR_TYPES = [DomainError, NotFoundError, InvalidTransitionError, PlannerError, WorkspaceError]
OTHER_ERROR_TYPES = [RuntimeError, ValueError, KeyError, OSError, TypeError]


def check_call_tool_error_handling(error_type: type[Exception], message: str) -> None:
    error = error_type(message)

    def fail() -> object:
        raise error

    if error_type in USER_ERROR_TYPES:
        result = call_tool(fail)
        assert result == ToolFailure(message)
    else:
        with pytest.raises(error_type) as info:
            call_tool(fail)
        assert info.value is error


@given(error_type=st.sampled_from(USER_ERROR_TYPES + OTHER_ERROR_TYPES), message=any_text)
@example(error_type=InvalidTransitionError, message="スプリント #1 は approved のため計画を変更できません")
@example(error_type=RuntimeError, message="")
def test_property_call_tool_turns_only_user_errors_into_failures(
    error_type: type[Exception], message: str
) -> None:
    """P7: 利用者向けのエラーは、どんなメッセージでもそのメッセージを持つ ToolFailure になり、
    それ以外の例外は同じ例外オブジェクトのまま伝わること。"""
    check_call_tool_error_handling(error_type, message)
