from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from caucus_race.domain.status import (
    Actor,
    Author,
    SpikeOrigin,
    SpikeStatus,
    SprintStatus,
    StorySource,
    StoryStatus,
    TaskStatus,
)


def now() -> datetime:
    return datetime.now(UTC)


class Entity(BaseModel):
    """永続化対象のエンティティ。id は `ST-12` のような接頭辞付きの連番。"""

    model_config = ConfigDict(validate_assignment=True)

    id_prefix: ClassVar[str]

    id: str
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class Project(Entity):
    id_prefix: ClassVar[str] = "PRJ"

    name: str
    repo_path: Path
    description: str = ""
    base_branch: str = "main"
    max_stories_per_sprint: int = 5
    executor_config: dict[str, Any] = Field(default_factory=lambda: {"type": "fake"})


class Sprint(Entity):
    id_prefix: ClassVar[str] = "SPR"

    project_id: str
    number: int
    goal: str
    status: SprintStatus = SprintStatus.PLANNING
    branch: str | None = None
    plan_comment: str | None = None
    approved_at: datetime | None = None
    started_at: datetime | None = None
    reviewed_at: datetime | None = None
    closed_at: datetime | None = None


class ExecutionResult(BaseModel):
    status: Literal["done", "blocked", "error"]
    summary: str = ""
    blocked_reason: str | None = None
    error: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    proposals: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class Story(Entity):
    id_prefix: ClassVar[str] = "ST"

    project_id: str
    sprint_id: str | None = None
    title: str
    description: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    source: StorySource = StorySource.HUMAN
    source_ref: str | None = None
    priority: int = 0
    status: StoryStatus = StoryStatus.BACKLOG
    result: ExecutionResult | None = None
    start_sha: str | None = None
    commits: list[str] = Field(default_factory=list)
    wip_branch: str | None = None


class Task(Entity):
    id_prefix: ClassVar[str] = "TK"

    story_id: str
    title: str
    description: str = ""
    order: int = 0
    status: TaskStatus = TaskStatus.TODO


class Spike(Entity):
    id_prefix: ClassVar[str] = "SP"

    story_id: str
    question: str
    context: str = ""
    options: list[str] = Field(default_factory=list)
    recommendation: str | None = None
    answer: str | None = None
    raised_by: SpikeOrigin = SpikeOrigin.PLANNER
    status: SpikeStatus = SpikeStatus.OPEN


class Feedback(Entity):
    id_prefix: ClassVar[str] = "FB"

    sprint_id: str
    story_id: str | None = None
    body: str
    converted_story_id: str | None = None


class RetroNote(Entity):
    id_prefix: ClassVar[str] = "RT"

    sprint_id: str
    author: Author = Author.HUMAN
    body: str


class Event(Entity):
    id_prefix: ClassVar[str] = "EV"

    actor: Actor
    entity_type: str
    entity_id: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
