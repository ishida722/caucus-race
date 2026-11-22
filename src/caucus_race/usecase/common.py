from typing import Any

from caucus_race.domain.entities import Entity, Event, Project, Spike, Sprint, Story
from caucus_race.domain.errors import DomainError, NotFoundError
from caucus_race.domain.status import Actor, SpikeStatus, SprintStatus
from caucus_race.port.repository import Repository


def record(repo: Repository, actor: Actor, entity: Entity, kind: str, **payload: Any) -> None:
    """状態変更を監査ログに残す。呼び出し側のトランザクション内で呼ぶ。"""
    repo.add(
        Event(
            id=repo.next_id(Event),
            actor=actor,
            entity_type=type(entity).__name__,
            entity_id=entity.id,
            kind=kind,
            payload=payload,
        )
    )


def project_by_name(repo: Repository, name: str) -> Project:
    found = repo.query(Project, name=name)
    if not found:
        raise NotFoundError(f"プロジェクト {name} が見つかりません")
    return found[0]


def latest_sprint(repo: Repository, project: Project) -> Sprint | None:
    sprints = repo.query(Sprint, project_id=project.id)
    return max(sprints, key=lambda s: s.number) if sprints else None


def open_sprint(repo: Repository, project: Project) -> Sprint | None:
    """終了していない（closed 以外の）スプリント。1 プロジェクトに高々 1 つ。"""
    sprint = latest_sprint(repo, project)
    return sprint if sprint and sprint.status != SprintStatus.CLOSED else None


def require_open_sprint(repo: Repository, project: Project) -> Sprint:
    sprint = open_sprint(repo, project)
    if sprint is None:
        raise DomainError(f"{project.name} に進行中のスプリントがありません")
    return sprint


def sprint_stories(repo: Repository, sprint: Sprint) -> list[Story]:
    stories = repo.query(Story, sprint_id=sprint.id)
    return sorted(stories, key=lambda s: (s.priority, s.created_at))


def open_spikes(repo: Repository, stories: list[Story]) -> list[Spike]:
    if not stories:
        return []
    return repo.query(Spike, story_id=[s.id for s in stories], status=SpikeStatus.OPEN)


def story_to_refine(repo: Repository, spike: Spike) -> Story | None:
    """スパイクに回答した後、AI にストーリーを見直させるべきなら、そのストーリーを返す。

    AI プランナーが設定されていて、ストーリーの未解決のスパイクがなくなった場合に見直す。
    """
    story = repo.get(Story, spike.story_id)
    project = repo.get(Project, story.project_id)
    if project.planner_config.get("type", "manual") == "manual":
        return None
    return story
