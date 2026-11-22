import logging
from dataclasses import dataclass, field
from pathlib import Path

from injector import inject

from caucus_race.domain.entities import Feedback, Project, RetroNote, Spike, Sprint, Story, now
from caucus_race.domain.errors import DomainError, InvalidTransitionError
from caucus_race.domain.status import (
    Actor,
    Author,
    SpikeStatus,
    SprintStatus,
    StorySource,
    StoryStatus,
)
from caucus_race.port.planner import PlannerError
from caucus_race.port.repository import Repository
from caucus_race.port.workspace import Workspace, WorkspaceError
from caucus_race.settings import Settings
from caucus_race.usecase.ai_planning import AIPlanningService
from caucus_race.usecase.common import (
    latest_sprint,
    project_by_name,
    record,
    require_open_sprint,
    sprint_stories,
)
from caucus_race.usecase.project import BacklogService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReportExtras:
    """レビュー資料に載せる、DB の外から集めた情報。"""

    guide: str | None = None
    sprint_diff_stat: str = ""
    story_diff_stats: dict[str, str] = field(default_factory=dict)


def _code_block(text: str) -> list[str]:
    return ["```", text, "```"]


def render_report(
    project: Project,
    sprint: Sprint,
    stories: list[Story],
    spikes: list[Spike],
    extras: ReportExtras | None = None,
) -> str:
    extras = extras or ReportExtras()
    done = sum(1 for s in stories if s.status == StoryStatus.DONE)
    lines = [
        f"# {project.name} スプリント #{sprint.number} レビュー",
        "",
        f"- ゴール: {sprint.goal}",
        f"- ブランチ: `{sprint.branch}`（ベース: `{project.base_branch}`）",
        f"- ストーリー: {done} / {len(stories)} 件完了",
    ]
    if extras.guide:
        lines += ["", "## 確認手順（AI が作成）", "", extras.guide]
    if extras.sprint_diff_stat:
        lines += ["", "## 変更の概要", "", *_code_block(extras.sprint_diff_stat)]

    lines += ["", "## ストーリー"]
    for story in stories:
        result = story.result
        lines += ["", f"### {story.id}: {story.title}（{story.status}）"]
        if story.acceptance_criteria:
            lines.append("受け入れ条件:")
            lines += [f"- [ ] {c}" for c in story.acceptance_criteria]
        if result:
            if result.summary:
                lines += ["", f"要約: {result.summary}"]
            if result.blocked_reason:
                lines.append(f"中断理由: {result.blocked_reason}")
            if result.error:
                lines.append(f"エラー: {result.error}")
            if result.raw.get("log"):
                lines.append(f"実行ログ: `{result.raw['log']}`")
        if story.commits:
            lines.append(f"コミット: {', '.join(c[:8] for c in story.commits)}")
        if story.id in extras.story_diff_stats:
            lines += _code_block(extras.story_diff_stats[story.id])
        if story.wip_branch:
            lines.append(f"途中の変更: `{story.wip_branch}`")
        for spike in spikes:
            if spike.story_id == story.id and spike.status == SpikeStatus.OPEN:
                lines.append(f"未解決のスパイク {spike.id}: {spike.question}")

    proposals = [p for s in stories if s.result for p in s.result.proposals]
    if proposals:
        lines += ["", "## 実行器が挙げた範囲外の課題（バックログに追加済み）"]
        lines += [f"- [{p.kind}] {p.title}" for p in proposals]
    return "\n".join(lines) + "\n"


class ReviewService:
    @inject
    def __init__(
        self,
        repo: Repository,
        backlog: BacklogService,
        ai: AIPlanningService,
        workspace: Workspace,
        settings: Settings,
    ) -> None:
        self.repo = repo
        self.workspace = workspace
        self.ai = ai
        self.backlog = backlog
        self.report_dir = settings.data_dir / "reviews"

    def _review_sprint(self, project: Project) -> Sprint:
        sprint = require_open_sprint(self.repo, project)
        if sprint.status != SprintStatus.REVIEW:
            raise InvalidTransitionError(
                f"スプリント #{sprint.number} はレビュー待ちではありません（{sprint.status}）"
            )
        return sprint

    def report(self, project_name: str, *, regenerate_guide: bool = False) -> tuple[Path, str]:
        """レビュー資料を生成する。確認手順は AI に一度だけ書かせ、スプリントに保存して使い回す。"""
        project = project_by_name(self.repo, project_name)
        sprint = self._review_sprint(project)
        if sprint.review_guide is None or regenerate_guide:
            sprint = self._write_guide(project, sprint)
        stories = sprint_stories(self.repo, sprint)
        spikes = self.repo.query(Spike, story_id=[s.id for s in stories]) if stories else []
        extras = ReportExtras(
            guide=sprint.review_guide,
            sprint_diff_stat=self._diff_stat(project, project.base_branch, sprint.branch),
            story_diff_stats={
                s.id: stat
                for s in stories
                if s.start_sha
                and s.commits
                and (stat := self._diff_stat(project, s.start_sha, s.commits[-1]))
            },
        )
        text = render_report(project, sprint, stories, spikes, extras)
        path = self.report_dir / project.name / f"sprint-{sprint.number}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path, text

    def _write_guide(self, project: Project, sprint: Sprint) -> Sprint:
        try:
            guide = self.ai.review_guide(project, sprint)
        except PlannerError as e:
            logger.info("確認手順を作成しませんでした: %s", e)
            return sprint
        with self.repo.transaction():
            sprint = self.repo.get(Sprint, sprint.id)
            sprint.review_guide = guide
            self.repo.save(sprint)
        return sprint

    def _diff_stat(self, project: Project, base: str, head: str | None) -> str:
        if head is None:
            return ""
        try:
            return self.workspace.diff_stat(project.repo_path, base, head)
        except WorkspaceError as e:
            logger.warning("差分を取得できませんでした: %s", e)
            return ""

    def add_feedback(self, project_name: str, body: str, story_id: str | None = None) -> Feedback:
        with self.repo.transaction():
            sprint = self._review_sprint(project_by_name(self.repo, project_name))
            if story_id is not None:
                story = self.repo.get(Story, story_id)
                if story.sprint_id != sprint.id:
                    raise DomainError(f"{story_id} はスプリント #{sprint.number} のストーリーではありません")
            feedback = self.repo.add(
                Feedback(id=self.repo.next_id(Feedback), sprint_id=sprint.id, story_id=story_id, body=body)
            )
            record(self.repo, Actor.HUMAN, feedback, "feedback.added")
        return feedback

    def convert_feedback(
        self, feedback_id: str, title: str | None = None, actor: Actor = Actor.HUMAN
    ) -> Story:
        """フィードバックをバックログのストーリーに変換する。"""
        with self.repo.transaction():
            feedback = self.repo.get(Feedback, feedback_id)
            if feedback.converted_story_id:
                raise DomainError(f"{feedback_id} は {feedback.converted_story_id} に変換済みです")
            sprint = self.repo.get(Sprint, feedback.sprint_id)
            project = self.repo.get(Project, sprint.project_id)
            description = feedback.body
            if feedback.story_id:
                description += f"\n\n（{feedback.story_id} へのフィードバック）"
            story = self.backlog.add_story(
                project.name,
                title or feedback.body.splitlines()[0][:200],
                description=description,
                source=StorySource.FEEDBACK,
                source_ref=feedback.id,
                actor=actor,
            )
            feedback.converted_story_id = story.id
            self.repo.save(feedback)
        return story

    def close(self, project_name: str) -> Sprint:
        """レビューを完了する。未変換のフィードバックはストーリーに変換し、未完了のストーリーはバックログに戻す（R-6）。"""
        self._review_sprint(project_by_name(self.repo, project_name))
        # AI で変換する（失敗時は機械的に変換される）。残ったものは下で 1 件ずつ変換する
        self.ai.convert_pending_feedback(project_name)
        with self.repo.transaction():
            project = project_by_name(self.repo, project_name)
            sprint = self._review_sprint(project)
            for feedback in self.repo.query(Feedback, sprint_id=sprint.id):
                if feedback.converted_story_id is None:
                    self.convert_feedback(feedback.id, actor=Actor.SYSTEM)
            for story in sprint_stories(self.repo, sprint):
                if story.status in (StoryStatus.FAILED, StoryStatus.SUSPENDED):
                    story.status = StoryStatus.BACKLOG
                    story.sprint_id = None
                    self.repo.save(story)
                    record(self.repo, Actor.SYSTEM, story, "story.returned_to_backlog", sprint_id=sprint.id)
            sprint.status = SprintStatus.CLOSED
            sprint.closed_at = now()
            self.repo.save(sprint)
            record(self.repo, Actor.HUMAN, sprint, "sprint.closed")
        return sprint


class RetroService:
    @inject
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def add(self, project_name: str, body: str, author: Author = Author.HUMAN) -> RetroNote:
        with self.repo.transaction():
            sprint = latest_sprint(self.repo, project_by_name(self.repo, project_name))
            if sprint is None:
                raise DomainError("スプリントがありません")
            note = self.repo.add(
                RetroNote(id=self.repo.next_id(RetroNote), sprint_id=sprint.id, author=author, body=body)
            )
            record(self.repo, Actor.HUMAN if author == Author.HUMAN else Actor.PLANNER, note, "retro.added")
        return note

    def notes(self, project_name: str) -> list[RetroNote]:
        project = project_by_name(self.repo, project_name)
        sprint_ids = [s.id for s in self.repo.query(Sprint, project_id=project.id)]
        return self.repo.query(RetroNote, sprint_id=sprint_ids) if sprint_ids else []
