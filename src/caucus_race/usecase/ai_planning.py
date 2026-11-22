"""AI プランナーを使うユースケース。

プランナーの呼び出しは時間がかかるので DB のトランザクションの外で行い、
返ってきた下書きを検証してからトランザクション内で反映する。
"""

import logging

from injector import inject

from caucus_race.domain.entities import (
    Feedback,
    Project,
    RetroNote,
    Spike,
    Sprint,
    Story,
    Task,
)
from caucus_race.domain.errors import DomainError, InvalidTransitionError
from caucus_race.domain.status import (
    Actor,
    SpikeOrigin,
    SpikeStatus,
    SprintStatus,
    StorySource,
    StoryStatus,
)
from caucus_race.port.notifier import HumanActionRequired, Notifier
from caucus_race.port.planner import (
    FeedbackView,
    PlannedStoryDraft,
    PlannerError,
    PlannerFactory,
    PlanningContext,
    SpikeDraft,
    SprintPlanDraft,
    SprintView,
    StoryView,
    TaskDraft,
    convert_feedback_mechanically,
)
from caucus_race.port.repository import Repository
from caucus_race.usecase.common import (
    latest_sprint,
    open_sprint,
    project_by_name,
    record,
    sprint_stories,
)

logger = logging.getLogger(__name__)


class AIPlanningService:
    @inject
    def __init__(self, repo: Repository, planners: PlannerFactory, notifier: Notifier) -> None:
        self.repo = repo
        self.planners = planners
        self.notifier = notifier

    # --- 入力の組み立て ---

    def _story_view(self, story: Story) -> StoryView:
        spikes = self.repo.query(Spike, story_id=story.id, status=SpikeStatus.OPEN)
        tasks = sorted(self.repo.query(Task, story_id=story.id), key=lambda t: t.order)
        result = story.result
        return StoryView(
            id=story.id,
            title=story.title,
            description=story.description,
            acceptance_criteria=story.acceptance_criteria,
            decisions=story.decisions,
            source=story.source,
            status=story.status,
            open_spikes=[s.question for s in spikes],
            tasks=[t.title for t in tasks],
            result_summary=(result.summary or result.blocked_reason or result.error or "") if result else "",
        )

    def _sprint_view(self, sprint: Sprint) -> SprintView:
        return SprintView(
            number=sprint.number,
            goal=sprint.goal,
            stories=[self._story_view(s) for s in sprint_stories(self.repo, sprint)],
            feedback=[f.body for f in self.repo.query(Feedback, sprint_id=sprint.id)],
            retro_notes=[n.body for n in self.repo.query(RetroNote, sprint_id=sprint.id)],
        )

    def _context(self, project: Project, current: Sprint | None = None) -> PlanningContext:
        backlog = sorted(
            self.repo.query(Story, project_id=project.id, status=StoryStatus.BACKLOG),
            key=lambda s: (s.priority, s.created_at),
        )
        closed = [
            s
            for s in self.repo.query(Sprint, project_id=project.id, status=SprintStatus.CLOSED)
            if current is None or s.id != current.id
        ]
        previous = max(closed, key=lambda s: s.number) if closed else None
        return PlanningContext(
            project_name=project.name,
            project_description=project.description,
            max_stories=project.max_stories_per_sprint,
            backlog=[self._story_view(s) for s in backlog],
            previous_sprint=self._sprint_view(previous) if previous else None,
            current_plan=self._sprint_view(current) if current else None,
            reject_comment=current.plan_comment if current else None,
        )

    # --- スプリント計画 ---

    def check_plannable(self, project_name: str) -> tuple[Project, Sprint | None]:
        """AI に計画を作らせられる状態かを確かめ、プロジェクトと計画中のスプリント（なければ None）を返す。

        終了していないスプリントが planning 以外（承認後・実行中・レビュー中）なら計画を変更できない（R-3）。
        """
        project = project_by_name(self.repo, project_name)
        current = open_sprint(self.repo, project)
        if current is not None and current.status != SprintStatus.PLANNING:
            raise DomainError(f"スプリント #{current.number}（{current.status}）が終了していません")
        return project, current

    def plan(self, project_name: str) -> Sprint:
        """AI にスプリント計画を作らせる。差し戻された計画があれば作り直させる。"""
        project, current = self.check_plannable(project_name)

        planner = self.planners.for_project(project)
        draft = planner.create_sprint_plan(self._context(project, current))

        with self.repo.transaction():
            if current is None:
                last = latest_sprint(self.repo, project)
                sprint = self.repo.add(
                    Sprint(
                        id=self.repo.next_id(Sprint),
                        project_id=project.id,
                        number=(last.number + 1) if last else 1,
                        goal=draft.goal,
                    )
                )
                record(self.repo, Actor.PLANNER, sprint, "sprint.created", goal=draft.goal)
            else:
                sprint = current
                self._clear_plan(sprint)
                sprint.goal = draft.goal
                sprint.plan_comment = None
                self.repo.save(sprint)
            self._apply_plan(project, sprint, draft)
            record(self.repo, Actor.PLANNER, sprint, "sprint.planned", rationale=draft.rationale)

        open_count = len(
            [
                s
                for st in sprint_stories(self.repo, sprint)
                for s in self.repo.query(Spike, story_id=st.id, status=SpikeStatus.OPEN)
            ]
        )
        self.notifier.notify(
            HumanActionRequired(
                kind="plan_ready",
                project=project.name,
                message=(
                    f"スプリント #{sprint.number} の計画を確認してください"
                    f"（未解決のスパイク {open_count} 件）"
                ),
            )
        )
        return sprint

    def _clear_plan(self, sprint: Sprint) -> None:
        """再計画の前に、現在の計画のストーリーをバックログに戻す。回答済みのスパイクと決定事項は残す。"""
        for story in sprint_stories(self.repo, sprint):
            for spike in self.repo.query(Spike, story_id=story.id, status=SpikeStatus.OPEN):
                if spike.raised_by == SpikeOrigin.PLANNER:
                    spike.status = SpikeStatus.DROPPED
                    self.repo.save(spike)
            story.sprint_id = None
            story.status = StoryStatus.BACKLOG
            self.repo.save(story)
            record(self.repo, Actor.PLANNER, story, "story.unplanned", sprint_id=sprint.id)

    def _apply_plan(self, project: Project, sprint: Sprint, draft: SprintPlanDraft) -> None:
        drafts = draft.stories
        if len(drafts) > project.max_stories_per_sprint:
            logger.warning(
                "計画のストーリー数が上限を超えたため、先頭の %d 件だけを採用します",
                project.max_stories_per_sprint,
            )
            drafts = drafts[: project.max_stories_per_sprint]

        applied = 0
        for d in drafts:
            story = self._resolve_story(project, d)
            if story is None:
                continue
            story.sprint_id = sprint.id
            story.status = StoryStatus.PLANNED
            self.repo.save(story)
            record(self.repo, Actor.PLANNER, story, "story.planned", sprint_id=sprint.id)
            if d.tasks:
                self._replace_tasks(story, d.tasks)
            self._add_spikes(story, d.spikes)
            applied += 1
        if applied == 0:
            raise PlannerError("計画に採用できるストーリーがありませんでした")

    def _resolve_story(self, project: Project, d: PlannedStoryDraft) -> Story | None:
        if d.story_id is None:
            story = self.repo.add(
                Story(
                    id=self.repo.next_id(Story),
                    project_id=project.id,
                    title=d.title,
                    description=d.description,
                    acceptance_criteria=d.acceptance_criteria,
                    source=StorySource.AI,
                    priority=len(self.repo.query(Story, project_id=project.id)),
                )
            )
            record(self.repo, Actor.PLANNER, story, "story.added", source=story.source)
            return story

        story = self.repo.find(Story, d.story_id)
        if story is None or story.project_id != project.id or story.status != StoryStatus.BACKLOG:
            logger.warning("計画のストーリー %s はバックログにないため採用しません", d.story_id)
            return None
        if d.description:
            story.description = d.description
        if d.acceptance_criteria:
            story.acceptance_criteria = d.acceptance_criteria
        return story

    def _replace_tasks(self, story: Story, tasks: list[TaskDraft]) -> None:
        for task in self.repo.query(Task, story_id=story.id):
            self.repo.delete(Task, task.id)
        for order, t in enumerate(tasks):
            task = self.repo.add(
                Task(
                    id=self.repo.next_id(Task),
                    story_id=story.id,
                    title=t.title,
                    description=t.description,
                    order=order,
                )
            )
            record(self.repo, Actor.PLANNER, task, "task.added", story_id=story.id)

    def _add_spikes(self, story: Story, spikes: list[SpikeDraft]) -> None:
        for s in spikes:
            spike = self.repo.add(
                Spike(
                    id=self.repo.next_id(Spike),
                    story_id=story.id,
                    question=s.question,
                    context=s.context,
                    options=s.options,
                    recommendation=s.recommendation or None,
                    raised_by=SpikeOrigin.PLANNER,
                )
            )
            record(self.repo, Actor.PLANNER, spike, "spike.raised", story_id=story.id)

    # --- スパイク回答後の見直し ---

    def refine(self, story_id: str) -> Story:
        """スパイクの回答を踏まえて、受け入れ条件とタスクを AI に見直させる。"""
        story = self.repo.get(Story, story_id)
        self._check_refinable(story)
        project = self.repo.get(Project, story.project_id)
        planner = self.planners.for_project(project)
        draft = planner.refine_story(self._context(project), self._story_view(story))

        with self.repo.transaction():
            story = self.repo.get(Story, story_id)
            self._check_refinable(story)
            if draft.acceptance_criteria:
                story.acceptance_criteria = draft.acceptance_criteria
                self.repo.save(story)
            if draft.tasks:
                self._replace_tasks(story, draft.tasks)
            self._add_spikes(story, draft.spikes)
            record(self.repo, Actor.PLANNER, story, "story.refined", new_spikes=len(draft.spikes))
        if draft.spikes:
            self.notifier.notify(
                HumanActionRequired(
                    kind="spike_raised",
                    project=project.name,
                    message=f"{story.id} に新しいスパイクが {len(draft.spikes)} 件あります",
                )
            )
        return story

    def _check_refinable(self, story: Story) -> None:
        if story.status == StoryStatus.BACKLOG:
            return
        if story.status == StoryStatus.PLANNED and story.sprint_id:
            sprint = self.repo.get(Sprint, story.sprint_id)
            if sprint.status in (SprintStatus.PLANNING, SprintStatus.APPROVED):
                return
        raise InvalidTransitionError(f"{story.id} は見直しできる状態ではありません（{story.status}）")

    # --- レビュー ---

    def review_guide(self, project: Project, sprint: Sprint) -> str:
        """スプリントの成果物を人間が確認する手順を AI に書かせる。"""
        planner = self.planners.for_project(project)
        draft = planner.write_review_guide(self._context(project, sprint))
        return draft.to_markdown()

    # --- フィードバックの変換 ---

    def convert_pending_feedback(self, project_name: str) -> list[Story]:
        """レビュー中のスプリントの未変換のフィードバックをストーリーに変換する。

        AI プランナーが失敗した場合は、機械的な変換に切り替える。
        """
        project = project_by_name(self.repo, project_name)
        sprint = open_sprint(self.repo, project)
        if sprint is None or sprint.status != SprintStatus.REVIEW:
            raise InvalidTransitionError("レビュー中のスプリントがありません")
        pending = [f for f in self.repo.query(Feedback, sprint_id=sprint.id) if f.converted_story_id is None]
        if not pending:
            return []
        views = [
            FeedbackView(
                id=f.id,
                body=f.body,
                story=self._story_view(self.repo.get(Story, f.story_id)) if f.story_id else None,
            )
            for f in pending
        ]
        context = self._context(project, sprint)
        try:
            draft = self.planners.for_project(project).convert_feedback(context, views)
        except PlannerError as e:
            logger.warning("AI によるフィードバックの変換に失敗したため、機械的に変換します: %s", e)
            draft = convert_feedback_mechanically(views)

        pending_by_id = {f.id: f for f in pending}
        created: list[Story] = []
        with self.repo.transaction():
            for d in draft.stories:
                ids = [i for i in d.feedback_ids if i in pending_by_id]
                if not ids:
                    continue
                story = self.repo.add(
                    Story(
                        id=self.repo.next_id(Story),
                        project_id=project.id,
                        title=d.title,
                        description=d.description,
                        acceptance_criteria=d.acceptance_criteria,
                        source=StorySource.FEEDBACK,
                        source_ref=",".join(ids),
                        priority=len(self.repo.query(Story, project_id=project.id)),
                    )
                )
                record(self.repo, Actor.PLANNER, story, "story.added", source=story.source)
                for i in ids:
                    feedback = pending_by_id[i]
                    if feedback.converted_story_id is None:
                        feedback.converted_story_id = story.id
                        self.repo.save(feedback)
                created.append(story)
        return created
