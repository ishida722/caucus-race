"""MCP で公開する、バックログの起票とスプリント計画のツール。

MCP の SDK には依存しない。server.py がこれらのメソッドを MCP のツールとして登録する。
ルール違反（DomainError など）は例外のまま投げる。server.py は call_tool で呼び、ルール違反を
ToolFailure（エラーの結果 isError）に変える。メッセージは CLI の「エラー: ...」と同じ内容になる。
"""

from collections.abc import Callable
from dataclasses import dataclass

from injector import inject
from pydantic import BaseModel, Field

from caucus_race.domain.entities import Project, Spike, Sprint, Story, Task
from caucus_race.domain.errors import DomainError, NotFoundError
from caucus_race.domain.status import SpikeStatus
from caucus_race.plan_job import PlanJobs, PlanJobState
from caucus_race.port.planner import PlannerError
from caucus_race.port.repository import Repository
from caucus_race.port.workspace import WorkspaceError
from caucus_race.usecase.ai_planning import AIPlanningService
from caucus_race.usecase.common import project_by_name, sprint_stories
from caucus_race.usecase.planning import PlanningService
from caucus_race.usecase.project import BacklogService

# 利用者（エージェント）に理由を見せるエラー。CLI の「エラー: ...」と同じ型
USER_ERRORS = (NotFoundError, WorkspaceError, PlannerError)  # ミュータント: ルール違反を変換しない


@dataclass(frozen=True)
class ToolFailure:
    """ツールがルール違反などで失敗したことを表す。server.py がエラーの結果（isError）に変える。"""

    message: str


def call_tool[R](fn: Callable[[], R]) -> R | ToolFailure:
    """ツールの関数を呼び、利用者に見せるエラーを ToolFailure に変える。

    それ以外の例外（想定外の不具合）は包まずにそのまま伝える。MCP では SDK が汎用のエラーにし、
    トレースバックは stderr のログに出る。
    """
    try:
        return fn()
    except USER_ERRORS as e:
        return ToolFailure(str(e))


class StoryResult(BaseModel):
    id: str
    title: str
    description: str
    acceptance_criteria: list[str]
    status: str


class SprintStoryItem(BaseModel):
    id: str
    title: str
    tasks: list[str]
    open_spikes: list[str]


class SprintResult(BaseModel):
    project: str
    number: int
    goal: str
    status: str
    stories: list[SprintStoryItem]


class TaskResult(BaseModel):
    id: str
    story_id: str
    title: str
    description: str


class SpikeResult(BaseModel):
    id: str
    story_id: str
    question: str
    context: str
    options: list[str]
    recommendation: str | None


class PlanJobResult(BaseModel):
    project: str
    status: str = Field(description="not_started / running / succeeded / failed")
    message: str
    started_at: str | None = None
    finished_at: str | None = None
    sprint_number: int | None = None
    goal: str | None = None
    story_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    log_path: str | None = None


def _job_result(project: str, state: PlanJobState | None) -> PlanJobResult:
    if state is None:
        return PlanJobResult(project=project, status="not_started", message="計画の作成は実行されていません")
    messages = {
        "running": "AI が計画を作成しています。しばらくしてから get_plan_status で確認してください",
        "succeeded": (
            f"スプリント #{state.sprint_number} の計画を作成しました。人間の確認（承認）を待っています"
        ),
        "failed": f"計画を作成できませんでした: {state.error}",
    }
    return PlanJobResult(
        project=project,
        status=state.status,
        message=messages[state.status],
        started_at=state.started_at.isoformat(),
        finished_at=state.finished_at.isoformat() if state.finished_at else None,
        sprint_number=state.sprint_number,
        goal=state.goal,
        story_ids=state.story_ids,
        error=state.error,
        log_path=str(state.log_path) if state.log_path else None,
    )


class PlanningTools:
    @inject
    def __init__(
        self,
        repo: Repository,
        backlog: BacklogService,
        planning: PlanningService,
        ai: AIPlanningService,
        jobs: PlanJobs,
    ) -> None:
        self.repo = repo
        self.backlog = backlog
        self.planning = planning
        self.ai = ai
        self.jobs = jobs

    # --- 表示用の変換 ---

    def _sprint_result(self, project: Project, sprint: Sprint) -> SprintResult:
        sprint = self.repo.get(Sprint, sprint.id)
        items = []
        for story in sprint_stories(self.repo, sprint):
            tasks = sorted(self.repo.query(Task, story_id=story.id), key=lambda t: t.order)
            spikes = self.repo.query(Spike, story_id=story.id, status=SpikeStatus.OPEN)
            items.append(
                SprintStoryItem(
                    id=story.id,
                    title=story.title,
                    tasks=[t.title for t in tasks],
                    open_spikes=[f"{s.id}: {s.question}" for s in spikes],
                )
            )
        return SprintResult(
            project=project.name, number=sprint.number, goal=sprint.goal, status=sprint.status, stories=items
        )

    def _story_project(self, story_id: str) -> Project:
        return self.repo.get(Project, self.repo.get(Story, story_id).project_id)

    def _ensure_not_planning(self, project_name: str) -> None:
        """AI が計画を作成している間は、計画を変更させない（完了時に AI の計画で置き換わるため）。"""
        if self.jobs.running(project_name):
            raise DomainError(
                f"{project_name} は AI がスプリント計画を作成中のため変更できません。"
                " get_plan_status で完了を確認してから操作してください"
            )

    # --- バックログ ---

    def add_story(
        self,
        project: str,
        title: str,
        description: str = "",
        acceptance_criteria: list[str] | None = None,
    ) -> StoryResult:
        story = self.backlog.add_story(
            project, title, description=description, acceptance_criteria=acceptance_criteria or []
        )
        return StoryResult(
            id=story.id,
            title=story.title,
            description=story.description,
            acceptance_criteria=story.acceptance_criteria,
            status=story.status,
        )

    # --- スプリント（人間・エージェントによる計画） ---

    def create_sprint(self, project: str, goal: str) -> SprintResult:
        self._ensure_not_planning(project)
        sprint = self.planning.new_sprint(project, goal)
        return self._sprint_result(project_by_name(self.repo, project), sprint)

    def add_story_to_sprint(self, project: str, story_id: str) -> SprintResult:
        self._ensure_not_planning(project)
        story = self.planning.add_story(project, story_id)
        return self._sprint_result(
            project_by_name(self.repo, project), self.repo.get(Sprint, story.sprint_id)
        )

    def remove_story_from_sprint(self, project: str, story_id: str) -> SprintResult:
        self._ensure_not_planning(project)
        sprint_id = self.repo.get(Story, story_id).sprint_id
        self.planning.remove_story(project, story_id)
        return self._sprint_result(project_by_name(self.repo, project), self.repo.get(Sprint, sprint_id))

    def add_task(self, story_id: str, title: str, description: str = "") -> TaskResult:
        self._ensure_not_planning(self._story_project(story_id).name)
        task = self.planning.add_task(story_id, title, description)
        return TaskResult(id=task.id, story_id=task.story_id, title=task.title, description=task.description)

    def add_spike(
        self,
        story_id: str,
        question: str,
        context: str = "",
        options: list[str] | None = None,
        recommendation: str | None = None,
    ) -> SpikeResult:
        self._ensure_not_planning(self._story_project(story_id).name)
        spike = self.planning.add_spike(
            story_id, question, context=context, options=options or [], recommendation=recommendation
        )
        return SpikeResult(
            id=spike.id,
            story_id=spike.story_id,
            question=spike.question,
            context=spike.context,
            options=spike.options,
            recommendation=spike.recommendation,
        )

    # --- AI による計画 ---

    def plan_sprint(self, project: str) -> PlanJobResult:
        """計画できる状態かをその場で確かめてから、AI による計画の作成を別プロセスで始める。"""
        p, _ = self.ai.check_plannable(project)
        if p.planner_config.get("type", "manual") == "manual":
            raise DomainError(
                f"{project} には AI プランナーが設定されていません"
                f"（caucus project set {project} --planner claude）"
            )
        return _job_result(project, self.jobs.start(project))

    def get_plan_status(self, project: str) -> PlanJobResult:
        project_by_name(self.repo, project)
        return _job_result(project, self.jobs.status(project))
