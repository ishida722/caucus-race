"""テスト・動作確認用の偽実行器。"""

from collections.abc import Callable
from pathlib import Path

from caucus_race.domain.entities import ExecutionResult, Project
from caucus_race.port.executor import Brief, Executor

type Scenario = Callable[[Brief, Path], ExecutionResult]


def write_note(brief: Brief, workdir: Path) -> ExecutionResult:
    """`.caucus-fake/<story_id>.md` にブリーフを書き出して done を返す。"""
    note = workdir / ".caucus-fake" / f"{brief.story_id}.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(brief.to_markdown(), encoding="utf-8")
    return ExecutionResult(
        status="done",
        summary=f"{brief.story_id} を偽実行器で処理しました",
        changed_files=[str(note.relative_to(workdir))],
    )


class FakeExecutor:
    """ストーリー ID ごとのシナリオに従って結果を返す。未登録のストーリーは default で処理する。"""

    def __init__(
        self,
        scenarios: dict[str, Scenario] | None = None,
        default: Scenario = write_note,
    ) -> None:
        self.scenarios = scenarios or {}
        self.default = default
        self.calls: list[Brief] = []

    def run(self, brief: Brief, workdir: Path) -> ExecutionResult:
        self.calls.append(brief)
        return self.scenarios.get(brief.story_id, self.default)(brief, workdir)


class SingleExecutorFactory:
    """どのプロジェクトにも同じ実行器を返す。"""

    def __init__(self, executor: Executor) -> None:
        self.executor = executor

    def for_project(self, project: Project) -> Executor:
        return self.executor
