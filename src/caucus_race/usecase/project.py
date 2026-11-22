from pathlib import Path

from injector import inject

from caucus_race.domain.entities import Project, Story
from caucus_race.domain.errors import DomainError
from caucus_race.domain.status import Actor, StorySource, StoryStatus
from caucus_race.port.repository import Repository
from caucus_race.usecase.common import project_by_name, record


class ProjectService:
    @inject
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def register(
        self,
        name: str,
        repo_path: Path,
        *,
        description: str = "",
        base_branch: str = "main",
        max_stories_per_sprint: int = 5,
        executor: str = "fake",
    ) -> Project:
        if not (repo_path / ".git").exists():
            raise DomainError(f"{repo_path} は git リポジトリではありません")
        with self.repo.transaction():
            if self.repo.query(Project, name=name):
                raise DomainError(f"プロジェクト {name} は登録済みです")
            project = self.repo.add(
                Project(
                    id=self.repo.next_id(Project),
                    name=name,
                    repo_path=repo_path.resolve(),
                    description=description,
                    base_branch=base_branch,
                    max_stories_per_sprint=max_stories_per_sprint,
                    executor_config={"type": executor},
                )
            )
            record(self.repo, Actor.HUMAN, project, "project.registered")
        return project

    def list_projects(self) -> list[Project]:
        return self.repo.query(Project)


class BacklogService:
    @inject
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def add_story(
        self,
        project_name: str,
        title: str,
        *,
        description: str = "",
        acceptance_criteria: list[str] | None = None,
        source: StorySource = StorySource.HUMAN,
        source_ref: str | None = None,
        actor: Actor = Actor.HUMAN,
    ) -> Story:
        with self.repo.transaction():
            project = project_by_name(self.repo, project_name)
            priority = len(self.repo.query(Story, project_id=project.id))
            story = self.repo.add(
                Story(
                    id=self.repo.next_id(Story),
                    project_id=project.id,
                    title=title,
                    description=description,
                    acceptance_criteria=acceptance_criteria or [],
                    source=source,
                    source_ref=source_ref,
                    priority=priority,
                )
            )
            record(self.repo, actor, story, "story.added", source=source)
        return story

    def backlog(self, project_name: str) -> list[Story]:
        project = project_by_name(self.repo, project_name)
        stories = self.repo.query(Story, project_id=project.id, status=StoryStatus.BACKLOG)
        return sorted(stories, key=lambda s: (s.priority, s.created_at))
