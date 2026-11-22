from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from caucus_race.domain.entities import ExecutionResult, Project


class Brief(BaseModel):
    """実行器に渡すストーリー単位の作業指示。"""

    story_id: str
    title: str
    goal: str
    decisions: list[str] = Field(default_factory=list)
    tasks: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)

    def to_markdown(self) -> str:
        def section(title: str, items: list[str], empty: str = "（なし）") -> str:
            body = "\n".join(f"- {i}" for i in items) if items else empty
            return f"# {title}\n{body}\n"

        return "\n".join(
            [
                f"# 目的\n{self.story_id}: {self.title}\n\n{self.goal}\n",
                section("決定事項（設計し直さないこと）", self.decisions),
                section("作業内容", self.tasks),
                section("受け入れ条件", self.acceptance_criteria),
                section("触らない範囲", self.out_of_scope),
            ]
        )


class Executor(Protocol):
    def run(self, brief: Brief, workdir: Path) -> ExecutionResult: ...


class ExecutorFactory(Protocol):
    """プロジェクトの設定（executor_config）に応じた実行器を返す。"""

    def for_project(self, project: Project) -> Executor: ...
