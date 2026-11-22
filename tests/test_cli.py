from pathlib import Path

import pytest
from typer.testing import CliRunner

from caucus_race.cli import main as cli

runner = CliRunner()


@pytest.fixture(autouse=True)
def caucus_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAUCUS_HOME", str(tmp_path / "home"))
    cli.container.cache_clear()


def invoke(*args: str) -> str:
    result = runner.invoke(cli.app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_cli_walkthrough(git_repo: Path) -> None:
    invoke("project", "add", "demo", "--repo", str(git_repo))
    invoke("story", "add", "demo", "一覧画面", "-a", "一覧が表示される")
    invoke("sprint", "new", "demo", "--goal", "一覧が見られる")
    invoke("sprint", "add", "demo", "ST-1")
    invoke("spike", "add", "ST-1", "並び順は？", "-o", "新しい順", "-o", "古い順")
    assert "要対応: スパイク回答 1 件, 計画の承認" in invoke("status")
    invoke("spike", "answer", "SP-1", "新しい順")
    invoke("sprint", "approve", "demo")
    assert "review" in invoke("sprint", "start", "demo")
    assert (git_repo / ".caucus-fake" / "ST-1.md").exists()
    assert "一覧画面" in invoke("review", "show", "demo")
    invoke("feedback", "add", "demo", "件数も表示したい")
    invoke("review", "done", "demo")
    assert "件数も表示したい" in invoke("story", "list", "demo")


def test_cli_reports_domain_errors(git_repo: Path) -> None:
    invoke("project", "add", "demo", "--repo", str(git_repo))
    result = runner.invoke(cli.app, ["sprint", "approve", "demo"])
    assert result.exit_code != 0
