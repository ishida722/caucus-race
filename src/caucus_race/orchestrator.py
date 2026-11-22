"""スプリント実行のオーケストレーター。

DB の状態だけを見て次の一手を決めるので、途中で止まっても `run` を呼び直せば再開できる。
"""

import logging

from injector import inject

from caucus_race.domain.entities import (
    ExecutionResult,
    Project,
    Proposal,
    Spike,
    Sprint,
    Story,
    Task,
    now,
)
from caucus_race.domain.errors import DomainError
from caucus_race.domain.status import (
    Actor,
    SpikeOrigin,
    SprintStatus,
    StorySource,
    StoryStatus,
    TaskStatus,
)
from caucus_race.port.executor import Brief, ExecutorFactory
from caucus_race.port.notifier import HumanActionRequired, Notifier
from caucus_race.port.repository import Repository
from caucus_race.port.workspace import Workspace
from caucus_race.usecase.common import record, sprint_stories

logger = logging.getLogger(__name__)

STORY_TRAILER = "Caucus-Story"


def build_brief(story: Story, tasks: list[Task]) -> Brief:
    goal = story.description or story.title
    return Brief(
        story_id=story.id,
        title=story.title,
        goal=goal,
        decisions=story.decisions,
        tasks=[t.title + (f": {t.description}" if t.description else "") for t in tasks],
        acceptance_criteria=story.acceptance_criteria,
    )


class Orchestrator:
    @inject
    def __init__(
        self,
        repo: Repository,
        executors: ExecutorFactory,
        workspace: Workspace,
        notifier: Notifier,
    ) -> None:
        self.repo = repo
        self.executors = executors
        self.workspace = workspace
        self.notifier = notifier

    def run(self, sprint_id: str) -> Sprint:
        while True:
            sprint = self.repo.get(Sprint, sprint_id)
            if sprint.status != SprintStatus.ACTIVE:
                return sprint
            project = self.repo.get(Project, sprint.project_id)
            stories = sprint_stories(self.repo, sprint)

            for story in stories:
                if story.status == StoryStatus.RUNNING:
                    self._recover(project, sprint, story)

            story = next((s for s in stories if s.status == StoryStatus.QUEUED), None)
            if story is None:
                self._finish(project, sprint)
                continue
            self._run_story(project, sprint, story)

    # --- 内部処理 ---

    def _commit_message(self, story: Story, subject: str) -> str:
        return f"{subject}\n\n{STORY_TRAILER}: {story.id}"

    def _ensure_on_branch(self, project: Project, sprint: Sprint) -> None:
        assert sprint.branch is not None
        if self.workspace.current_branch(project.repo_path) != sprint.branch:
            if not self.workspace.is_clean(project.repo_path):
                raise DomainError(
                    f"{project.repo_path} が {sprint.branch} 以外のブランチで変更中のため続行できません"
                )
            self.workspace.checkout(project.repo_path, sprint.branch)

    def _preserve_and_reset(self, project: Project, story: Story) -> None:
        """未完了のストーリーの変更を退避ブランチに保存し、実行前の状態に戻す。"""
        repo_path = project.repo_path
        assert story.start_sha is not None
        self.workspace.commit_all(repo_path, self._commit_message(story, f"WIP {story.id}: {story.title}"))
        if self.workspace.head(repo_path) != story.start_sha:
            wip = f"caucus/wip/{story.id}"
            self.workspace.save_to_branch(repo_path, wip)
            story.wip_branch = wip
        self.workspace.reset_hard(repo_path, story.start_sha)

    def _recover(self, project: Project, sprint: Sprint, story: Story) -> None:
        """running のまま止まっていたストーリーを退避・巻き戻して再実行の対象にする。"""
        logger.warning("%s は実行中に停止していたため再実行します", story.id)
        if story.start_sha is not None:
            self._ensure_on_branch_for_recovery(project, sprint)
            self._preserve_and_reset(project, story)
        with self.repo.transaction():
            story.status = StoryStatus.QUEUED
            self.repo.save(story)
            record(self.repo, Actor.SYSTEM, story, "story.recovered", wip_branch=story.wip_branch)

    def _ensure_on_branch_for_recovery(self, project: Project, sprint: Sprint) -> None:
        # 停止時の作業途中の変更は、スプリント用ブランチ上にあるはずなので checkout しない
        assert sprint.branch is not None
        current = self.workspace.current_branch(project.repo_path)
        if current != sprint.branch:
            raise DomainError(
                f"{project.repo_path} が {sprint.branch} ではなく {current} にあるため復旧できません"
            )

    def _run_story(self, project: Project, sprint: Sprint, story: Story) -> None:
        repo_path = project.repo_path
        self._ensure_on_branch(project, sprint)
        if not self.workspace.is_clean(repo_path):
            raise DomainError(f"{repo_path} に想定外の未コミットの変更があるため続行できません")

        with self.repo.transaction():
            story.start_sha = self.workspace.head(repo_path)
            story.status = StoryStatus.RUNNING
            self.repo.save(story)
            record(self.repo, Actor.SYSTEM, story, "story.started", start_sha=story.start_sha)

        logger.info("%s: %s を実行します", story.id, story.title)
        tasks = sorted(self.repo.query(Task, story_id=story.id), key=lambda t: t.order)
        executor = self.executors.for_project(project)
        try:
            result = executor.run(build_brief(story, tasks), repo_path)
        except Exception as e:  # 実行器の不具合でスプリント全体を止めない
            logger.exception("%s の実行中に例外が発生しました", story.id)
            result = ExecutionResult(status="error", error=f"{type(e).__name__}: {e}")

        current = self.workspace.current_branch(repo_path)
        if current != sprint.branch:
            result = result.model_copy(
                update={
                    "status": "error",
                    "error": f"実行器がブランチを {sprint.branch} から {current} に切り替えました",
                }
            )
            if self.workspace.is_clean(repo_path):
                self.workspace.checkout(repo_path, sprint.branch)  # type: ignore[arg-type]
            else:
                raise DomainError(result.error or "")

        self._apply_result(project, sprint, story, tasks, result)

    def _apply_result(
        self,
        project: Project,
        sprint: Sprint,
        story: Story,
        tasks: list[Task],
        result: ExecutionResult,
    ) -> None:
        repo_path = project.repo_path
        assert story.start_sha is not None

        if result.status == "done":
            self.workspace.commit_all(repo_path, self._commit_message(story, f"{story.id}: {story.title}"))
            story.commits = self.workspace.commits_between(
                repo_path, story.start_sha, self.workspace.head(repo_path)
            )
            story.status = StoryStatus.DONE
        else:
            self._preserve_and_reset(project, story)
            story.status = StoryStatus.SUSPENDED if result.status == "blocked" else StoryStatus.FAILED

        logger.info("%s: %s（%s）", story.id, story.status, result.summary or result.error or "")
        with self.repo.transaction():
            story.result = result
            self.repo.save(story)
            record(
                self.repo,
                Actor.EXECUTOR,
                story,
                f"story.{story.status}",
                summary=result.summary,
                commits=story.commits,
                wip_branch=story.wip_branch,
            )
            if result.status == "done":
                for task in tasks:
                    task.status = TaskStatus.DONE
                    self.repo.save(task)
            if result.status == "blocked":
                spike = self.repo.add(
                    Spike(
                        id=self.repo.next_id(Spike),
                        story_id=story.id,
                        question=result.blocked_reason or "実行器が判断できずに停止しました",
                        context=result.summary,
                        raised_by=SpikeOrigin.EXECUTOR,
                    )
                )
                record(self.repo, Actor.EXECUTOR, spike, "spike.raised", story_id=story.id)
            for proposal in result.proposals:
                self._add_proposal(project, story, proposal)

        if result.status == "blocked":
            self.notifier.notify(
                HumanActionRequired(
                    kind="spike_raised",
                    project=project.name,
                    message=f"{story.id} が中断しました: {result.blocked_reason}",
                )
            )

    def _add_proposal(self, project: Project, story: Story, proposal: Proposal) -> None:
        priority = len(self.repo.query(Story, project_id=project.id))
        new = self.repo.add(
            Story(
                id=self.repo.next_id(Story),
                project_id=project.id,
                title=proposal.title.strip()[:200] or "（無題の提案）",
                description=f"{proposal.body}\n\n（{story.id} の実行中に挙がった {proposal.kind}）".strip(),
                source=StorySource.EXECUTOR_PROPOSAL,
                source_ref=story.id,
                priority=priority,
            )
        )
        record(self.repo, Actor.EXECUTOR, new, "story.added", source=new.source, proposal_kind=proposal.kind)

    def _finish(self, project: Project, sprint: Sprint) -> None:
        """全ストーリーが終了したのでレビューに進める（R-5）。"""
        with self.repo.transaction():
            sprint.status = SprintStatus.REVIEW
            sprint.reviewed_at = now()
            self.repo.save(sprint)
            record(self.repo, Actor.SYSTEM, sprint, "sprint.review_ready")
        self.notifier.notify(
            HumanActionRequired(
                kind="review_ready",
                project=project.name,
                message=f"スプリント #{sprint.number} のレビューを待っています",
            )
        )
