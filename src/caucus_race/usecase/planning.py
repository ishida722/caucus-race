from injector import inject

from caucus_race.domain.entities import Spike, Sprint, Story, Task, now
from caucus_race.domain.errors import DomainError, InvalidTransitionError
from caucus_race.domain.status import (
    Actor,
    SpikeOrigin,
    SpikeStatus,
    SprintStatus,
    StoryStatus,
)
from caucus_race.port.repository import Repository
from caucus_race.usecase.common import (
    latest_sprint,
    open_sprint,
    project_by_name,
    record,
    require_open_sprint,
    sprint_stories,
)


class PlanningService:
    @inject
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    # --- スプリント ---

    def new_sprint(self, project_name: str, goal: str, actor: Actor = Actor.HUMAN) -> Sprint:
        with self.repo.transaction():
            project = project_by_name(self.repo, project_name)
            current = open_sprint(self.repo, project)
            if current is not None:
                raise DomainError(f"スプリント #{current.number}（{current.status}）が終了していません")
            last = latest_sprint(self.repo, project)
            sprint = self.repo.add(
                Sprint(
                    id=self.repo.next_id(Sprint),
                    project_id=project.id,
                    number=(last.number + 1) if last else 1,
                    goal=goal,
                )
            )
            record(self.repo, actor, sprint, "sprint.created", goal=goal)
        return sprint

    def _planning_sprint(self, project_name: str) -> Sprint:
        sprint = require_open_sprint(self.repo, project_by_name(self.repo, project_name))
        if sprint.status != SprintStatus.PLANNING:
            raise InvalidTransitionError(
                f"スプリント #{sprint.number} は {sprint.status} のため計画を変更できません。"
                " 変更するには差し戻してください（R-3）"
            )
        return sprint

    def add_story(self, project_name: str, story_id: str, actor: Actor = Actor.HUMAN) -> Story:
        with self.repo.transaction():
            project = project_by_name(self.repo, project_name)
            sprint = self._planning_sprint(project_name)
            story = self.repo.get(Story, story_id)
            if story.project_id != project.id:
                raise DomainError(f"{story_id} は {project_name} のストーリーではありません")
            if story.status != StoryStatus.BACKLOG:
                raise InvalidTransitionError(f"{story_id} はバックログにありません（{story.status}）")
            if len(sprint_stories(self.repo, sprint)) >= project.max_stories_per_sprint:
                raise DomainError(
                    f"1 スプリントのストーリー数の上限（{project.max_stories_per_sprint}）に達しています"
                )
            story.sprint_id = sprint.id
            story.status = StoryStatus.PLANNED
            self.repo.save(story)
            record(self.repo, actor, story, "story.planned", sprint_id=sprint.id)
        return story

    def remove_story(self, project_name: str, story_id: str, actor: Actor = Actor.HUMAN) -> Story:
        with self.repo.transaction():
            sprint = self._planning_sprint(project_name)
            story = self.repo.get(Story, story_id)
            if story.sprint_id != sprint.id:
                raise DomainError(f"{story_id} はスプリント #{sprint.number} に含まれていません")
            story.sprint_id = None
            story.status = StoryStatus.BACKLOG
            self.repo.save(story)
            record(self.repo, actor, story, "story.unplanned", sprint_id=sprint.id)
        return story

    def approve(self, project_name: str) -> Sprint:
        with self.repo.transaction():
            sprint = self._planning_sprint(project_name)
            if not sprint_stories(self.repo, sprint):
                raise DomainError("ストーリーが 1 件もないスプリントは承認できません")
            sprint.status = SprintStatus.APPROVED
            sprint.approved_at = now()
            sprint.plan_comment = None
            self.repo.save(sprint)
            record(self.repo, Actor.HUMAN, sprint, "sprint.approved")
        return sprint

    def reject(self, project_name: str, comment: str) -> Sprint:
        with self.repo.transaction():
            sprint = require_open_sprint(self.repo, project_by_name(self.repo, project_name))
            if sprint.status not in (SprintStatus.PLANNING, SprintStatus.APPROVED):
                raise InvalidTransitionError(f"スプリント #{sprint.number} は {sprint.status} です")
            sprint.status = SprintStatus.PLANNING
            sprint.approved_at = None
            sprint.plan_comment = comment
            self.repo.save(sprint)
            record(self.repo, Actor.HUMAN, sprint, "sprint.rejected", comment=comment)
        return sprint

    # --- タスク・スパイク ---

    def _editable_story(self, story_id: str) -> Story:
        story = self.repo.get(Story, story_id)
        if story.status == StoryStatus.BACKLOG:
            return story
        if story.status == StoryStatus.PLANNED and story.sprint_id:
            sprint = self.repo.get(Sprint, story.sprint_id)
            if sprint.status == SprintStatus.PLANNING:
                return story
        raise InvalidTransitionError(f"{story_id} は計画中ではないため変更できません（{story.status}）")

    def add_task(self, story_id: str, title: str, description: str = "", actor: Actor = Actor.HUMAN) -> Task:
        with self.repo.transaction():
            story = self._editable_story(story_id)
            order = len(self.repo.query(Task, story_id=story.id))
            task = self.repo.add(
                Task(
                    id=self.repo.next_id(Task),
                    story_id=story.id,
                    title=title,
                    description=description,
                    order=order,
                )
            )
            record(self.repo, actor, task, "task.added", story_id=story.id)
        return task

    def add_spike(
        self,
        story_id: str,
        question: str,
        *,
        context: str = "",
        options: list[str] | None = None,
        recommendation: str | None = None,
        actor: Actor = Actor.HUMAN,
    ) -> Spike:
        with self.repo.transaction():
            story = self._editable_story(story_id)
            spike = self.repo.add(
                Spike(
                    id=self.repo.next_id(Spike),
                    story_id=story.id,
                    question=question,
                    context=context,
                    options=options or [],
                    recommendation=recommendation,
                    raised_by=SpikeOrigin.PLANNER,
                )
            )
            record(self.repo, actor, spike, "spike.raised", story_id=story.id)
        return spike

    def answer_spike(self, spike_id: str, answer: str) -> Spike:
        """回答をストーリーの決定事項に追加する（R-4）。"""
        with self.repo.transaction():
            spike = self.repo.get(Spike, spike_id)
            if spike.status != SpikeStatus.OPEN:
                raise InvalidTransitionError(f"{spike_id} は回答待ちではありません（{spike.status}）")
            spike.answer = answer
            spike.status = SpikeStatus.ANSWERED
            self.repo.save(spike)
            story = self.repo.get(Story, spike.story_id)
            story.decisions = [*story.decisions, f"{spike.question} → {answer}"]
            self.repo.save(story)
            record(self.repo, Actor.HUMAN, spike, "spike.answered", answer=answer)
        return spike

    def drop_spike(self, spike_id: str, reason: str = "") -> Spike:
        with self.repo.transaction():
            spike = self.repo.get(Spike, spike_id)
            if spike.status != SpikeStatus.OPEN:
                raise InvalidTransitionError(f"{spike_id} は回答待ちではありません（{spike.status}）")
            spike.status = SpikeStatus.DROPPED
            self.repo.save(spike)
            record(self.repo, Actor.HUMAN, spike, "spike.dropped", reason=reason)
        return spike
