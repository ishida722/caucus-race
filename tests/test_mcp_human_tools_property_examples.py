"""ST-3 のプロパティテスト（PH1〜PH6）の性質を、例の入力で確かめる（Hypothesis がなくても毎回動く）。

性質の本体と例の入力は `tests/human_tools_property_checks.py` にある。Hypothesis で生成した入力で確かめる版は
`tests/test_mcp_human_tools_properties.py`（Hypothesis が未導入の間は skip される）。
例には、過去に問題になった入力と境界の入力（`../`・`/`・`.`・`..`・長い非 ASCII・サロゲート・切り詰めの前後・
さまざまな空白・別のストーリーにだけ残る未解決のスパイク・type を省略したプランナーの設定など）を含める。
"""

from pathlib import Path

import pytest

from caucus_race.orchestrator import Orchestrator
from caucus_race.usecase.sprint import SprintService
from tests import human_tools_property_checks as C
from tests.test_mcp_human_tools import Env, P


@pytest.fixture
def env(tmp_path: Path, git_repo: Path) -> Env:
    return Env(tmp_path, git_repo)


@pytest.mark.parametrize(("project_name", "story_id"), C.REFINE_LOG_EXAMPLES)
def test_PH1_examples(project_name: str, story_id: str) -> None:
    C.check_refine_log_is_single_file_directly_under_refine_dir(project_name, story_id)


@pytest.mark.parametrize(("a", "b"), C.DISTINCT_PAIR_EXAMPLES)
def test_PH2_examples(a: tuple[str, str], b: tuple[str, str]) -> None:
    C.check_distinct_pairs_get_distinct_refine_logs(a, b)


@pytest.mark.parametrize(("comment", "blank"), C.REJECT_COMMENT_EXAMPLES)
def test_PH3_examples(env: Env, comment: str, blank: bool) -> None:
    C.check_reject_plan_comment(env, comment, blank)


@pytest.mark.parametrize(("answer", "same_story", "other_story", "planner"), C.ANSWER_SPIKE_EXAMPLES)
def test_PH4_examples(
    env: Env, answer: str, same_story: list[str], other_story: list[str], planner: int
) -> None:
    C.check_answer_spike(env, answer, same_story, other_story, planner)


@pytest.mark.parametrize("layout", C.LIST_SPIKES_EXAMPLES)
def test_PH5_examples(env: Env, layout: list[tuple[int, list[str]]]) -> None:
    C.check_list_spikes(env, layout)


@pytest.mark.parametrize(("body", "with_story"), C.ADD_FEEDBACK_EXAMPLES)
def test_PH6_examples(env: Env, body: str, with_story: bool) -> None:
    story = env.backlog.add_story(P, "一覧画面")
    env.planning.new_sprint(P, "一覧が見られる")
    env.planning.add_story(P, story.id)
    env.planning.approve(P)
    sprint = env.injector.get(SprintService).start(P)
    env.injector.get(Orchestrator).run(sprint.id)

    C.check_add_feedback(env, story.id, body, with_story)
