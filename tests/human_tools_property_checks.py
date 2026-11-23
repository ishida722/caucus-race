"""人間の判断にあたる操作のツール（HumanTools）と BackgroundRunner の性質
（ST-3 のプロパティテスト PH1〜PH6）。

設計表: docs/test-design/20261123-caucus-sprint-1.md の「## プロパティテスト」の「ST-3」。

性質を確かめる本体（`check_*`）と、必ず確かめる入力の例（`*_EXAMPLES`）だけを置き、Hypothesis には依存しない。
- `tests/test_mcp_human_tools_properties.py`: Hypothesis で生成した入力（と例）で
  `check_*` を呼ぶ。Hypothesis が未導入の間は理由付きで skip される
- `tests/test_mcp_human_tools_property_examples.py`: 例の入力で `check_*` を呼ぶ。
  Hypothesis がなくても毎回動く

このモジュールの名前は `test_` で始まらないため、pytest はテストとして集めない。
"""

import itertools
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from caucus_race.domain.entities import Feedback, Spike, Story
from caucus_race.domain.errors import DomainError
from caucus_race.domain.status import SpikeStatus, SprintStatus
from caucus_race.runner import BackgroundRunner
from caucus_race.usecase.common import latest_sprint, project_by_name
from caucus_race.usecase.project import ProjectService
from tests.test_mcp_human_tools import Env, FakeSpawn, P

# ファイル名の長さの上限（多くのファイルシステムで 255 バイト）
NAME_MAX = 255

# プランナーの設定（契約上ありうるもの）と、AI プランナーが設定済みか（見直しの条件のモデル）。
# type を省略した設定は manual 扱い（`caucus project set --planner` の既定）。manual・claude 以外の type は、
# 対応していないプランナーで、見直しを起動すべきかが仕様不明のため含めない
PLANNER_CONFIGS: list[tuple[dict[str, Any], bool]] = [
    ({"type": "claude"}, True),
    ({"type": "claude", "model": "claude-sonnet-5-5"}, True),
    ({"type": "manual"}, False),
    ({"model": "claude-sonnet-5-5"}, False),
]

SPIKE_STATUSES = ["open", "answered", "dropped"]


# --- 時刻（プロセス境界）を固定する ---

FIXED_NOW = datetime(2026, 11, 23, 12, 0, 0)


# --- 例ごとに別のプロジェクトを作る ---

_names = itertools.count()


def new_project(env: Env, planner_config: dict[str, Any]) -> str:
    """前の例のデータに影響されないよう、例ごとに別のプロジェクトを作る。"""
    name = f"prop-{next(_names)}"
    repo = Path(env.settings.data_dir).parent / "target"
    env.injector.get(ProjectService).register(name, repo, planner_config=planner_config)
    return name


# --- PH1・PH2: 見直しのログのファイル名（範囲・単射） ---


def refine_launch(project_name: str, story_id: str) -> tuple[Path, Path, list[list[str]]]:
    """見直しを起動し、（log_dir, ログの保存先, 起動したコマンドの一覧）を返す。

    ログのファイルを実際に作る。
    """
    tmp = Path(tempfile.mkdtemp())
    log_dir = tmp / "logs"
    spawn = FakeSpawn()
    # 時刻（プロセス境界）を固定し、同じ秒に起動した場合を作る
    runner = BackgroundRunner(tmp / "run", log_dir, spawn, clock=lambda: FIXED_NOW)
    launched = runner.start_refine(project_name, story_id)
    launched.log_path.parent.mkdir(parents=True, exist_ok=True)
    launched.log_path.touch()  # ファイル名が長すぎる・不正なら OSError になる
    return log_dir, launched.log_path, [argv for argv, _ in spawn.calls]


def check_refine_log_is_single_file_directly_under_refine_dir(project_name: str, story_id: str) -> None:
    """どんなプロジェクト名とストーリーの ID でも、見直しのログは logs/refine/ の直下（README の記載）の
    1 つのファイルで、ファイル名は 255 バイト以下で実際に作成でき、起動するのはそのストーリーの見直し
    （`caucus plan refine <story_id>` 相当）であること。"""
    log_dir, log_path, commands = refine_launch(project_name, story_id)

    assert log_path.parent == log_dir / "refine"
    assert len(log_path.name.encode("utf-8", "surrogatepass")) <= NAME_MAX
    assert [p.name for p in (log_dir / "refine").iterdir()] == [log_path.name]
    [argv] = commands
    plan_at = argv.index("plan")
    assert argv[plan_at + 1] == "refine"  # caucus plan refine
    assert argv[-1] == story_id  # 最後の位置引数がストーリーの ID（オプションとして解釈される位置ではない）


def check_distinct_pairs_get_distinct_refine_logs(a: tuple[str, str], b: tuple[str, str]) -> None:
    """（プロジェクト名, ストーリーの ID）の組が異なれば、同じ時刻（同じ秒）に起動しても見直しのログの
    ファイル名は異なること（別のプロジェクトや別のストーリーの見直しのログを、同じファイルに混ぜない）。"""
    assert a != b
    assert refine_launch(*a)[1].name != refine_launch(*b)[1].name


REFINE_LOG_EXAMPLES: list[tuple[str, str]] = [
    ("demo", "ST-1"),
    ("../escape", "ST-1"),
    ("a/b", "ST-1"),
    (".", "ST-1"),
    ("..", "ST-1"),
    ("記" * 1000, "ST-999999999999"),
    ("\ud800", "ST-1"),
    ("a" * 400, "ST-1"),
    ("A" * 150, "ST-77"),
    ("記" * 60, "ST-12345"),
]

DISTINCT_PAIR_EXAMPLES: list[tuple[tuple[str, str], tuple[str, str]]] = [
    (("a", "ST-1"), ("a", "ST-2")),
    (("a", "ST-1"), ("b", "ST-1")),
    (("a-%53%54", "ST-1"), ("a", "ST-1")),
    (("x-ST-5", "ST-1"), ("x", "ST-51")),
    (("A", "ST-1"), ("a", "ST-1")),
    (("a" * 200, "ST-1"), ("a" * 201, "ST-1")),
    (("\ud800", "ST-1"), ("%ED%A0%80", "ST-1")),
]


# --- PH3: 差し戻しのコメント（例外の性質・往復） ---


def ensure_approved(env: Env, project: str) -> None:
    sprint = latest_sprint(env.repo, project_by_name(env.repo, project))
    if sprint is None:
        story = env.backlog.add_story(project, "一覧画面")
        env.planning.new_sprint(project, "一覧が見られる")
        env.planning.add_story(project, story.id)
        sprint = latest_sprint(env.repo, project_by_name(env.repo, project))
    assert sprint is not None
    if sprint.status == SprintStatus.PLANNING:
        env.planning.approve(project)


def check_reject_plan_comment(env: Env, comment: str, blank: bool) -> None:
    """承認済みのスプリントを差し戻すとき、空白だけ（空を含む）のコメントは常に DomainError になって
    承認済みのまま・コメントも残らず、見える文字を含むコメントは常に受け付けられて計画中に戻り、
    その見える文字がコメントに残ること（前後に空白がなければ、コメントはそのままの形で残ること）。"""
    ensure_approved(env, P)
    before = latest_sprint(env.repo, project_by_name(env.repo, P))
    assert before is not None

    if blank:
        with pytest.raises(DomainError, match="コメントが必要"):
            env.tools.reject_plan(P, comment)
        after = latest_sprint(env.repo, project_by_name(env.repo, P))
        assert after is not None
        assert (after.status, after.plan_comment) == (SprintStatus.APPROVED, before.plan_comment)
        return

    env.tools.reject_plan(P, comment)
    after = latest_sprint(env.repo, project_by_name(env.repo, P))
    assert after is not None and after.status == SprintStatus.PLANNING
    assert after.plan_comment is not None and comment.strip() in after.plan_comment
    if comment == comment.strip():
        assert after.plan_comment == comment


REJECT_COMMENT_EXAMPLES: list[tuple[str, bool]] = [
    ("", True),
    ("　\n\t", True),
    (" ", True),
    ("\x85   ", True),
    ("a", False),
    (" a ", False),
    ("要", False),
    ("ストーリーを減らしてほしい\n2 行目", False),
]


# --- PH4: スパイクの回答（往復・不変条件・モデルとの一致） ---


def check_answer_spike(
    env: Env, answer: str, same_story: list[str], other_story: list[str], planner: int
) -> None:
    """どんな回答でも、ストーリーの決定事項は既存のものを残したまま 1 件だけ増え、
    その項目（と返り値の decision）は回答をそのまま含むこと。
    見直しを起動するのは「AI プランナーが設定済み」かつ
    「同じストーリーに未解決のスパイクが残らない」ときだけで、
    返り値の remaining_spikes は同じストーリーの未解決のスパイクと一致すること。
    同じストーリーの他のスパイクと、同じプロジェクトの別のストーリーのスパイクの状態の任意の組み合わせについて、
    この単純なモデルと一致すること（別のストーリーの未解決のスパイクは影響しない）。"""
    config, ai_planner = PLANNER_CONFIGS[planner]
    project = new_project(env, config)
    story = env.backlog.add_story(project, "一覧画面")
    target = env.planning.add_spike(story.id, "並び順は？")
    open_ids = add_spikes(env, story.id, same_story)
    other = env.backlog.add_story(project, "別のストーリー")
    add_spikes(env, other.id, other_story)
    before = env.repo.get(Story, story.id).decisions
    spawned_before = len(env.spawn.calls)

    result = env.tools.answer_spike(target.id, answer)

    after = env.repo.get(Story, story.id).decisions
    assert after[: len(before)] == before and len(after) == len(before) + 1
    assert answer in after[-1] and result.decision == after[-1]
    assert env.repo.get(Spike, target.id).status == SpikeStatus.ANSWERED
    expected_refine = ai_planner and not open_ids
    assert result.refine_started is expected_refine
    assert len(env.spawn.calls) - spawned_before == (1 if expected_refine else 0)
    assert set(result.remaining_spikes) == open_ids


def add_spikes(env: Env, story_id: str, statuses: list[str]) -> set[str]:
    """ストーリーに、指定した状態のスパイクを起票する。未解決（open）のスパイクの ID を返す。"""
    open_ids: set[str] = set()
    for i, status in enumerate(statuses):
        spike = env.planning.add_spike(story_id, f"質問 {i}")
        if status == "open":
            open_ids.add(spike.id)
        elif status == "answered":
            env.planning.answer_spike(spike.id, f"回答 {i}")
        else:
            env.planning.drop_spike(spike.id)
    return open_ids


# (answer, 同じストーリーの他のスパイク, 別のストーリーのスパイク, PLANNER_CONFIGS の番号)
ANSWER_SPIKE_EXAMPLES: list[tuple[str, list[str], list[str], int]] = [
    ("", [], [], 0),
    ("新しい順。\n固定記事は先頭 📌", ["dropped", "answered"], [], 0),
    ("x", ["open"], [], 0),
    ("x", [], ["open", "open"], 0),  # 別のストーリーにだけ未解決が残る
    ("x", [], [], 1),
    ("x", [], [], 2),
    ("x", [], [], 3),  # type を省略（manual 扱い）
    ("y", ["open", "open", "dropped"], ["open"], 2),
    ("z", ["answered"] * 3, ["dropped"], 0),
]


# --- PH5: 未解決のスパイクの一覧（モデルとの一致） ---


def check_list_spikes(env: Env, layout: list[tuple[int, list[str]]]) -> None:
    """プロジェクトのストーリー（とそのスパイクの状態）の任意の構成で、list_spikes は、そのプロジェクトの
    未解決のスパイクをちょうどすべて返すこと（回答済み・取り下げ済み・別のプロジェクトのスパイクを含めない）。
    layout の各要素は（どちらのプロジェクトか 0 / 1, そのストーリーのスパイクの状態のリスト）。"""
    projects = [new_project(env, {"type": "claude"}), new_project(env, {"type": "claude"})]
    expected: set[str] = set()
    for owner, statuses in layout:
        story = env.backlog.add_story(projects[owner], "ストーリー")
        open_ids = add_spikes(env, story.id, statuses)
        if owner == 0:
            expected |= open_ids

    result = env.tools.list_spikes(projects[0])

    assert sorted(s.id for s in result) == sorted(expected)  # 重複なく、ちょうどすべて


LIST_SPIKES_EXAMPLES: list[list[tuple[int, list[str]]]] = [
    [],
    [(1, ["open"])],
    [(0, ["open", "answered", "dropped"]), (1, ["open"]), (0, ["open"])],
    [(0, [])],
    [(0, ["dropped"] * 3)],
]


# --- PH6: フィードバックの登録（往復・不変条件） ---


def check_add_feedback(env: Env, story_id: str, body: str, with_story: bool) -> None:
    """レビュー待ちのとき、どんな本文でも、フィードバックはちょうど 1 件増え、本文はそのまま、
    ストーリーの指定は指定したとおり（指定しなければなし）で保存されること。"""
    before = len(env.repo.query(Feedback))

    result = env.tools.add_feedback(P, body, story_id if with_story else None)

    saved = env.repo.get(Feedback, result.id)
    assert len(env.repo.query(Feedback)) == before + 1
    assert (saved.body, saved.story_id) == (body, story_id if with_story else None)


ADD_FEEDBACK_EXAMPLES: list[tuple[str, bool]] = [
    ("", False),
    ("件数も表示したい。\n- 100 件を超えたら省略 ✂", True),
    ("x" * 5000, False),
]
