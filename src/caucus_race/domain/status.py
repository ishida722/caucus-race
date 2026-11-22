from enum import StrEnum


class SprintStatus(StrEnum):
    PLANNING = "planning"
    APPROVED = "approved"
    ACTIVE = "active"
    REVIEW = "review"
    CLOSED = "closed"


class StoryStatus(StrEnum):
    BACKLOG = "backlog"
    PLANNED = "planned"
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SUSPENDED = "suspended"


# スプリント実行の終了状態（R-5）
STORY_FINISHED = frozenset({StoryStatus.DONE, StoryStatus.FAILED, StoryStatus.SUSPENDED})


class StorySource(StrEnum):
    HUMAN = "human"
    AI = "ai"
    FEEDBACK = "feedback"
    EXECUTOR_PROPOSAL = "executor_proposal"


class TaskStatus(StrEnum):
    TODO = "todo"
    DONE = "done"
    SKIPPED = "skipped"


class SpikeStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    DROPPED = "dropped"


class SpikeOrigin(StrEnum):
    PLANNER = "planner"
    EXECUTOR = "executor"


class Actor(StrEnum):
    HUMAN = "human"
    PLANNER = "planner"
    EXECUTOR = "executor"
    SYSTEM = "system"


class Author(StrEnum):
    HUMAN = "human"
    AI = "ai"
