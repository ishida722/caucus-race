"""人間の判断にあたる操作のツール（HumanTools）と BackgroundRunner の境界値テスト（ST-3）。

境界値表は docs/test-design/20261123-caucus-sprint-1.md の「## 境界値テスト」の HB1〜HB21 にある。
ケースの ID には境界の名前と位置（on / off）を入れる。

モックするのはプロセスの起動（spawn）だけ（tests/test_mcp_human_tools.py の FakeSpawn）。
"""

from datetime import datetime
from pathlib import Path

import pytest

from caucus_race.domain.entities import Story
from caucus_race.domain.errors import DomainError
from caucus_race.domain.status import SprintStatus
from caucus_race.runner import BackgroundRunner, refine_argv
from caucus_race.usecase.common import latest_sprint, project_by_name
from tests.test_mcp_human_tools import MANUAL, Env, FakeSpawn, P

# 多くのファイルシステム（APFS・ext4）の、1 つのファイル名の長さの上限（バイト）
NAME_MAX = 255
FIXED_NOW = datetime(2026, 11, 23, 12, 0, 0)


@pytest.fixture
def env(tmp_path: Path, git_repo: Path) -> Env:
    return Env(tmp_path, git_repo)


def planning_story(env: Env, project: str = P) -> Story:
    story = env.backlog.add_story(project, "一覧画面")
    env.planning.new_sprint(project, "一覧が見られる")
    env.planning.add_story(project, story.id)
    return story


# --- 差し戻しコメントの長さ（空白を除いた長さ 0 と 1 の境目） ---


@pytest.mark.parametrize(
    "comment",
    [
        pytest.param("", id="HB1-length0-off"),
        pytest.param(" ", id="HB2-one-ascii-space-off"),
        pytest.param("　", id="HB3-one-fullwidth-space-off"),
        pytest.param(" \t\n", id="HB4-mixed-whitespace-off"),
    ],
)
def test_HB1_4_reject_plan_with_no_visible_character_fails(env: Env, comment: str) -> None:
    """空白を除くと 0 文字になるコメント（空・半角空白 1 文字・全角空白 1 文字・空白類の混在）のとき、
    差し戻しは例外になり、スプリントは承認済みのままであること（差し戻しはコメント必須）。"""
    planning_story(env)
    env.planning.approve(P)

    with pytest.raises(DomainError):
        env.tools.reject_plan(P, comment)

    sprint = latest_sprint(env.repo, project_by_name(env.repo, P))
    assert sprint is not None and sprint.status == SprintStatus.APPROVED


@pytest.mark.parametrize(
    "comment",
    [
        pytest.param("a", id="HB5-one-visible-char-on"),
        pytest.param("要", id="HB6-one-non-ascii-char-on"),
        pytest.param(" a ", id="HB7-one-visible-char-in-spaces-on"),
    ],
)
def test_HB5_7_reject_plan_with_one_visible_character_succeeds(env: Env, comment: str) -> None:
    """空白以外の文字がちょうど 1 文字あるコメントのとき、差し戻しが受け付けられ、
    スプリントは計画中に戻り、コメントが残ること（前後に空白がなければそのままの形で残ること）。"""
    planning_story(env)
    env.planning.approve(P)

    env.tools.reject_plan(P, comment)

    sprint = latest_sprint(env.repo, project_by_name(env.repo, P))
    assert sprint is not None and sprint.status == SprintStatus.PLANNING
    assert sprint.plan_comment is not None
    if comment == comment.strip():
        # 前後に空白がない入力は、取り除くかどうかの問題が起きないため、そのまま残ることを確かめる
        assert sprint.plan_comment == comment
    else:
        # 前後の空白を保存時に取り除くかどうかは仕様不明のため、空白以外の文字が残ることだけを確かめる
        assert comment.strip() in sprint.plan_comment


# --- 見直しの条件: ストーリーの未解決のスパイクの件数（0 件と 1 件の境目） ---


@pytest.mark.parametrize(
    ("others", "expected_refine"),
    [
        pytest.param([], True, id="HB8-remaining0-on"),
        pytest.param(["open"], False, id="HB9-remaining1-off"),
        pytest.param(["open", "open"], False, id="HB10-remaining2-off"),
        pytest.param(["dropped"], True, id="HB11-other-dropped-counts-as-0"),
        pytest.param(["answered"], True, id="HB12-other-answered-counts-as-0"),
    ],
)
def test_HB8_12_refine_starts_only_when_open_spikes_of_story_reach_zero(
    env: Env, others: list[str], expected_refine: bool
) -> None:
    """AI プランナーが設定済みのとき、回答後にそのストーリーの未解決のスパイクが 0 件なら見直しを起動し、
    1 件以上残れば起動しないこと。取り下げ済み・回答済みのスパイクは未解決に数えないこと。
    返り値の remaining_spikes は、残っている未解決のスパイクの件数と一致すること。"""
    story = planning_story(env)
    target = env.planning.add_spike(story.id, "並び順は？")
    remaining: list[str] = []
    for i, kind in enumerate(others):
        spike = env.planning.add_spike(story.id, f"質問 {i}")
        if kind == "open":
            remaining.append(spike.id)
        elif kind == "dropped":
            env.planning.drop_spike(spike.id)
        else:
            env.planning.answer_spike(spike.id, "回答済み")

    result = env.tools.answer_spike(target.id, "新しい順")

    assert result.refine_started is expected_refine
    assert len(env.spawn.calls) == (1 if expected_refine else 0)
    assert sorted(result.remaining_spikes) == sorted(remaining)


def test_HB13_refine_starts_on_the_answer_that_brings_open_spikes_from_one_to_zero(env: Env) -> None:
    """未解決のスパイクが 2 件あるストーリーで順に回答したとき、1 件目（残り 2 → 1）では見直しを起動せず、
    2 件目（残り 1 → 0）でちょうど 1 回起動すること。"""
    story = planning_story(env)
    first = env.planning.add_spike(story.id, "並び順は？")
    second = env.planning.add_spike(story.id, "件数は？")

    after_first = env.tools.answer_spike(first.id, "新しい順")
    calls_after_first = len(env.spawn.calls)
    after_second = env.tools.answer_spike(second.id, "20 件")

    assert (after_first.refine_started, calls_after_first) == (False, 0)
    assert after_second.refine_started is True
    assert [argv for argv, _ in env.spawn.calls] == [refine_argv(story.id)]


# --- 返り値の decision: 決定事項の件数（0 件・1 件・複数件）と、追加した項目の位置 ---


@pytest.mark.parametrize(
    "prior",
    [
        pytest.param(0, id="HB14-prior-decisions-0"),
        pytest.param(1, id="HB15-prior-decisions-1"),
        pytest.param(2, id="HB16-prior-decisions-2"),
    ],
)
def test_HB14_16_decision_is_the_item_appended_by_this_answer(env: Env, prior: int) -> None:
    """ストーリーに決定事項が 0 件・1 件・2 件あるときに回答すると、既存の決定事項はそのまま先頭に残り、
    回答を含む項目が末尾に 1 件追加され、返り値の decision はその追加した項目であること（R-4）。"""
    story = planning_story(env, MANUAL)  # 見直しを起動しないプロジェクトで、決定事項だけを見る
    for i in range(prior):
        env.planning.answer_spike(env.planning.add_spike(story.id, f"以前の質問 {i}").id, f"以前の回答 {i}")
    before = env.repo.get(Story, story.id).decisions
    spike = env.planning.add_spike(story.id, "並び順は？")

    result = env.tools.answer_spike(spike.id, "新しい順")

    after = env.repo.get(Story, story.id).decisions
    assert len(after) == prior + 1
    assert after[:prior] == before
    assert "新しい順" in after[-1]
    assert result.decision == after[-1]


# --- 見直しのログのファイル名の長さ（ファイル名の長さの上限 255 バイト） ---
#
# 仕様から導ける境界は、ファイルシステムのファイル名の上限（255 バイト）だけ。プロジェクト名をそのまま使うと
# どの長さで上限を超えるか（入力の側の境界の位置）は、接尾辞の長さや符号化の方法という実装の詳細で決まる。
# そのため、境界の位置を実装から逆算して on / off の 2 点を置く代わりに、上限をまたぐ十分に広い範囲の長さを
# すべて試し、どの長さでもログを作れること・ログが重ならないことを確かめる。

# (文字, 試す長さの最大値): 符号化前の名前が 255 バイトを十分に超える範囲まで試す
NAME_SWEEPS = [
    pytest.param("a", 400, id="lowercase-ascii-1-to-400"),
    pytest.param("A", 150, id="uppercase-ascii-1-to-150"),
    pytest.param("記", 60, id="non-ascii-1-to-60"),
]


@pytest.mark.parametrize(
    "story_id", [pytest.param("ST-1", id="ST-1"), pytest.param("ST-99999", id="ST-99999")]
)
@pytest.mark.parametrize(("char", "max_len"), NAME_SWEEPS)
def test_HB17_19_refine_log_fits_name_max_for_every_project_name_length(
    tmp_path: Path, char: str, max_len: int, story_id: str
) -> None:
    """プロジェクト名の長さが 1 文字から、そのままではファイル名の上限（255 バイト）を十分に超える長さまでの
    どの長さでも（上限をまたぐ前後を含む）、見直しのログのファイル名は 255 バイト以下で、logs/refine/ の直下に
    実際に作成できること（見直しを起動するにはログを作れる必要がある）。"""
    log_dir = tmp_path / "logs"
    runner = BackgroundRunner(tmp_path / "run", log_dir, FakeSpawn())
    (log_dir / "refine").mkdir(parents=True)

    too_long: list[int] = []
    for length in range(1, max_len + 1):
        log_path = runner.start_refine(char * length, story_id).log_path
        assert log_path.parent == log_dir / "refine", length
        if len(log_path.name.encode("utf-8")) > NAME_MAX:
            too_long.append(length)
            continue
        log_path.touch()  # ファイル名が長すぎると OSError（ENAMETOOLONG）になる

    assert too_long == [], f"ファイル名が {NAME_MAX} バイトを超えたプロジェクト名の長さ: {too_long[:5]}..."


@pytest.mark.parametrize(("char", "max_len"), NAME_SWEEPS)
def test_HB20_refine_logs_of_different_project_names_never_collide(
    tmp_path: Path, char: str, max_len: int
) -> None:
    """長さだけが 1 文字ずつ違うプロジェクト名（ファイル名の上限をまたぐ前後で、先頭が共通になるもの）を
    同じストーリー・同じ時刻で起動しても、見直しのログのファイル名がすべて別になること
    （別のプロジェクトのログを同じファイルに混ぜない）。"""
    # 時刻（プロセス境界）を固定し、すべて同じ秒に起動した扱いにする
    runner = BackgroundRunner(tmp_path / "run", tmp_path / "logs", FakeSpawn(), clock=lambda: FIXED_NOW)

    names = {runner.start_refine(char * length, "ST-1").log_path.name for length in range(1, max_len + 1)}

    assert len(names) == max_len


def test_HB21_refine_log_fits_name_max_for_project_name_far_over_the_limit(tmp_path: Path) -> None:
    """プロジェクト名が上限を大きく超える（符号化前で 3000 バイト）ときも、見直しのログを作成できること。"""
    log_dir = tmp_path / "logs"
    runner = BackgroundRunner(tmp_path / "run", log_dir, FakeSpawn())

    log_path = runner.start_refine("記" * 1000, "ST-1").log_path

    assert len(log_path.name.encode("utf-8")) <= NAME_MAX
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch()
    assert log_path.exists()
