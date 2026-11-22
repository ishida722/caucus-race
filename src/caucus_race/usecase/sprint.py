from injector import inject

from caucus_race.domain.entities import Sprint, now
from caucus_race.domain.errors import DomainError, InvalidTransitionError
from caucus_race.domain.status import Actor, SprintStatus, StoryStatus
from caucus_race.port.repository import Repository
from caucus_race.port.workspace import Workspace
from caucus_race.usecase.common import (
    open_spikes,
    project_by_name,
    record,
    require_open_sprint,
    sprint_stories,
)


def sprint_branch(sprint: Sprint) -> str:
    return f"caucus/sprint-{sprint.number}"


class SprintService:
    @inject
    def __init__(self, repo: Repository, workspace: Workspace) -> None:
        self.repo = repo
        self.workspace = workspace

    def start(self, project_name: str, actor: Actor = Actor.HUMAN) -> Sprint:
        """開始条件（R-2）を確かめ、スプリント用ブランチを作成して active にする。"""
        with self.repo.transaction():
            project = project_by_name(self.repo, project_name)
            sprint = require_open_sprint(self.repo, project)
            if sprint.status != SprintStatus.APPROVED:
                raise InvalidTransitionError(
                    f"スプリント #{sprint.number} は承認されていません（{sprint.status}）"
                )
            stories = sprint_stories(self.repo, sprint)
            spikes = open_spikes(self.repo, stories)
            if spikes:
                ids = ", ".join(s.id for s in spikes)
                raise DomainError(f"未解決のスパイクがあるため開始できません: {ids}")

            repo_path = project.repo_path
            if not self.workspace.is_clean(repo_path):
                raise DomainError(f"{repo_path} に未コミットの変更があるため開始できません")
            branch = sprint_branch(sprint)
            if self.workspace.branch_exists(repo_path, branch):
                raise DomainError(f"ブランチ {branch} が既に存在します")
            self.workspace.create_branch(repo_path, branch, project.base_branch)

            for story in stories:
                story.status = StoryStatus.QUEUED
                self.repo.save(story)
            sprint.status = SprintStatus.ACTIVE
            sprint.branch = branch
            sprint.started_at = now()
            self.repo.save(sprint)
            record(self.repo, actor, sprint, "sprint.started", branch=branch)
        return sprint
