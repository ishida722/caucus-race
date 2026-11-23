"""CLI の `caucus spike answer` の見直しの分岐のテスト。

ST-3 で見直しの条件を `story_to_refine` に切り出した箇所を、CLI を通して確かめる。
CLI は見直しを同期で行う（MCP の answer_spike はバックグラウンドで起動する）。見直しの条件
（AI プランナーが設定済みで、ストーリーの未解決のスパイクがなくなった）と
`--refine/--no-refine` の組み合わせを見る。

AI プランナー（外部の claude）だけを偽のプランナーに差し替える（プロセス境界）。見直したかどうかは、
偽のプランナーが見直しを頼まれた回数で確かめる。
"""

from pathlib import Path

import pytest
from injector import Injector
from typer.testing import CliRunner

from caucus_race.adapter.executor.fake import FakeExecutor
from caucus_race.adapter.notifier.console import RecordingNotifier
from caucus_race.adapter.planner.fake import FakePlanner
from caucus_race.cli import main as cli
from caucus_race.container import AppModule
from caucus_race.settings import Settings
from caucus_race.usecase.planning import PlanningService
from caucus_race.usecase.project import BacklogService, ProjectService
from tests.conftest import FakeAdaptersModule

runner = CliRunner()


@pytest.mark.parametrize(
    ("planner_type", "remaining", "refine_flag", "expected_refine"),
    [
        pytest.param("claude", False, [], True, id="ai-planner-last-spike-default"),
        pytest.param("claude", False, ["--refine"], True, id="ai-planner-last-spike-refine-flag"),
        pytest.param("claude", False, ["--no-refine"], False, id="ai-planner-last-spike-no-refine"),
        pytest.param("claude", True, [], False, id="ai-planner-spike-remains"),
        pytest.param("manual", False, [], False, id="manual-planner-last-spike"),
    ],
)
def test_cli_spike_answer_refines_only_with_ai_planner_last_spike_and_refine_flag(
    tmp_path: Path,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    planner_type: str,
    remaining: bool,
    refine_flag: list[str],
    expected_refine: bool,
) -> None:
    """`caucus spike answer` は、AI プランナーが設定済みで、ストーリーの未解決のスパイクがなくなり、
    `--no-refine` が指定されていないときだけ AI にストーリーを見直させ（見直した旨を表示し）、
    それ以外では見直さないこと（CLI の docstring の仕様。ST-3 の切り出しの前後で変わらないこと）。"""
    planner = FakePlanner()
    injector = Injector(
        [
            AppModule(Settings(data_dir=tmp_path / "home")),
            FakeAdaptersModule(FakeExecutor(), RecordingNotifier(), planner),
        ]
    )
    monkeypatch.setattr(cli, "container", lambda: injector)
    injector.get(ProjectService).register("demo", git_repo, planner_config={"type": planner_type})
    story = injector.get(BacklogService).add_story("demo", "一覧画面")
    planning = injector.get(PlanningService)
    spike = planning.add_spike(story.id, "並び順は？")
    if remaining:
        planning.add_spike(story.id, "件数は？")

    result = runner.invoke(cli.app, ["spike", "answer", spike.id, "新しい順", *refine_flag])

    assert result.exit_code == 0, result.output
    assert f"{spike.id} に回答しました" in result.output
    assert len(planner.refined) == (1 if expected_refine else 0)
    assert (f"{story.id} を見直しました" in result.output) is expected_refine
