"""人間の判断にあたる操作のツール（HumanTools）と BackgroundRunner のプロパティテスト（ST-3）。

設計表: docs/test-design/20261123-caucus-sprint-1.md の「## プロパティテスト」の「ST-3」（PH1〜PH6）。
性質の本体と例の入力は `tests/human_tools_property_checks.py` にある。
ここでは Hypothesis で入力を生成して渡すだけ。

Hypothesis は開発用の依存に未追加（`uv add --dev hypothesis` が作業環境の権限で拒否された）。
追加されるまでは、このファイル全体が理由付きで skip される（`pytest -rs` で表示される）。
例の入力での確認は `tests/test_mcp_human_tools_property_examples.py` で毎回動く。

入力の文字列について（ST-2 のプロパティテストと同じ方針）:
- ファイル名だけを作る性質（PH1・PH2）では、プロジェクト名にサロゲートも含める
  （file_stem は任意の str を受け付ける）
- DB を通る性質（PH3〜PH6）では、サロゲートを生成しない（既存の永続化がサロゲートを保存できない）
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

hypothesis = pytest.importorskip(
    "hypothesis", reason="Hypothesis が未導入のため skip（uv add --dev hypothesis で追加すると動く）"
)

from hypothesis import HealthCheck, example, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from caucus_race.orchestrator import Orchestrator  # noqa: E402
from caucus_race.usecase.sprint import SprintService  # noqa: E402
from tests import human_tools_property_checks as C  # noqa: E402
from tests.test_mcp_human_tools import Env, P  # noqa: E402

# DB を通る性質で使う文字列（サロゲートを除く任意の文字）
DB_TEXT = st.text(alphabet=st.characters(exclude_categories=("Cs",)), max_size=200)
# 1 文字以上の、見える文字（文字・数字・記号・句読点）
VISIBLE_CHAR = st.characters(categories=("L", "N", "P", "S"))
# 空白: Unicode の空白の区分（Zs: 空白、Zl: 行区切り、Zp: 段落区切り）と、制御文字の空白
# （タブ・改行・VT・FF・CR・NEL）。ゼロ幅空白（Cf）と、情報分離文字（U+001C〜U+001F）は空白とみなすかが
# 仕様不明のため含めない
WHITESPACE_CHAR = st.one_of(
    st.characters(categories=("Zs", "Zl", "Zp")),
    st.sampled_from(["\t", "\n", "\x0b", "\x0c", "\r", "\x85"]),
)
# システムが振るストーリーの ID の形（ST-<連番>）。任意の ID の扱いは仕様不明（設計表）
STORY_ID = st.integers(min_value=1, max_value=10**12).map(lambda n: f"ST-{n}")
# ファイル名だけを作る性質で使うプロジェクト名（サロゲートを含む任意の str と、エンコードで長くなる非 ASCII）
PROJECT_NAME_ANY = st.one_of(
    st.text(min_size=1),
    st.text(alphabet=st.characters(min_codepoint=0x3000, max_codepoint=0x9FFF), min_size=1, max_size=100),
    st.text(alphabet=st.characters(categories=("Cs",)), min_size=1, max_size=5),
)
SPIKE_STATUS_LIST = st.lists(st.sampled_from(C.SPIKE_STATUSES), max_size=5)

# DB と git を使うテストは 1 件ごとに数十ミリ秒かかるため、件数を抑える
DB_SETTINGS = settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)


def with_examples(examples: list[Any], unpack: Callable[[Any], dict[str, Any]]) -> Callable[[Any], Any]:
    """`tests/human_tools_property_checks.py` の例の入力を、すべて `@example` として加える。"""

    def decorate(test: Any) -> Any:
        for value in examples:
            test = example(**unpack(value))(test)
        return test

    return decorate


@pytest.fixture
def env(tmp_path: Path, git_repo: Path) -> Env:
    return Env(tmp_path, git_repo)


@given(project_name=PROJECT_NAME_ANY, story_id=STORY_ID)
@with_examples(C.REFINE_LOG_EXAMPLES, lambda v: {"project_name": v[0], "story_id": v[1]})
def test_PH1_refine_log_is_single_file_directly_under_refine_dir(project_name: str, story_id: str) -> None:
    C.check_refine_log_is_single_file_directly_under_refine_dir(project_name, story_id)


@given(pair=st.lists(st.tuples(PROJECT_NAME_ANY, STORY_ID), min_size=2, max_size=2, unique=True).map(tuple))
@with_examples(C.DISTINCT_PAIR_EXAMPLES, lambda v: {"pair": v})
def test_PH2_distinct_pairs_get_distinct_refine_logs(pair: tuple[tuple[str, str], tuple[str, str]]) -> None:
    C.check_distinct_pairs_get_distinct_refine_logs(*pair)


BLANK_COMMENT = st.text(alphabet=WHITESPACE_CHAR, max_size=20)
VISIBLE_COMMENT = st.tuples(
    st.text(alphabet=WHITESPACE_CHAR, max_size=3),
    st.text(alphabet=VISIBLE_CHAR, min_size=1, max_size=50),
    DB_TEXT,
    st.text(alphabet=WHITESPACE_CHAR, max_size=3),
).map(lambda t: t[0] + t[1] + t[2] + t[3])


@DB_SETTINGS
@given(case=st.one_of(BLANK_COMMENT.map(lambda c: (c, True)), VISIBLE_COMMENT.map(lambda c: (c, False))))
@with_examples(C.REJECT_COMMENT_EXAMPLES, lambda v: {"case": v})
def test_PH3_reject_plan_comment(env: Env, case: tuple[str, bool]) -> None:
    C.check_reject_plan_comment(env, *case)


@DB_SETTINGS
@given(
    answer=DB_TEXT,
    same_story=SPIKE_STATUS_LIST,
    other_story=SPIKE_STATUS_LIST,
    planner=st.integers(min_value=0, max_value=len(C.PLANNER_CONFIGS) - 1),
)
@with_examples(
    C.ANSWER_SPIKE_EXAMPLES,
    lambda v: {"answer": v[0], "same_story": v[1], "other_story": v[2], "planner": v[3]},
)
def test_PH4_answer_spike(
    env: Env, answer: str, same_story: list[str], other_story: list[str], planner: int
) -> None:
    C.check_answer_spike(env, answer, same_story, other_story, planner)


@DB_SETTINGS
@given(
    layout=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=1), st.lists(st.sampled_from(C.SPIKE_STATUSES), max_size=4)
        ),
        max_size=5,
    )
)
@with_examples(C.LIST_SPIKES_EXAMPLES, lambda v: {"layout": v})
def test_PH5_list_spikes(env: Env, layout: list[tuple[int, list[str]]]) -> None:
    C.check_list_spikes(env, layout)


@pytest.fixture
def review_env(env: Env) -> tuple[Env, str]:
    """レビュー待ちのスプリントを 1 つ用意する（例ごとに作り直すと遅いため、全例で共有する）。"""
    story = env.backlog.add_story(P, "一覧画面")
    env.planning.new_sprint(P, "一覧が見られる")
    env.planning.add_story(P, story.id)
    env.planning.approve(P)
    sprint = env.injector.get(SprintService).start(P)
    env.injector.get(Orchestrator).run(sprint.id)
    return env, story.id


@DB_SETTINGS
@given(body=DB_TEXT, with_story=st.booleans())
@with_examples(C.ADD_FEEDBACK_EXAMPLES, lambda v: {"body": v[0], "with_story": v[1]})
def test_PH6_add_feedback(review_env: tuple[Env, str], body: str, with_story: bool) -> None:
    env, story_id = review_env
    C.check_add_feedback(env, story_id, body, with_story)
