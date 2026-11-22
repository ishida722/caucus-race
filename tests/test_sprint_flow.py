from pathlib import Path

import pytest
from injector import Injector

from caucus_race.adapter.executor.fake import FakeExecutor
from caucus_race.adapter.notifier.console import RecordingNotifier
from caucus_race.domain.entities import ExecutionResult, Proposal, Spike, Sprint, Story, Task
from caucus_race.domain.errors import DomainError, InvalidTransitionError
from caucus_race.domain.status import (
    SpikeOrigin,
    SpikeStatus,
    SprintStatus,
    StorySource,
    StoryStatus,
    TaskStatus,
)
from caucus_race.orchestrator import Orchestrator
from caucus_race.port.executor import Brief
from caucus_race.port.repository import Repository
from caucus_race.usecase.planning import PlanningService
from caucus_race.usecase.project import BacklogService, ProjectService
from caucus_race.usecase.review import RetroService, ReviewService
from caucus_race.usecase.sprint import SprintService
from tests.conftest import git

P = "demo"


class App:
    def __init__(self, injector: Injector) -> None:
        self.repo: Repository = injector.get(Repository)  # type: ignore[type-abstract]
        self.projects = injector.get(ProjectService)
        self.backlog = injector.get(BacklogService)
        self.planning = injector.get(PlanningService)
        self.sprints = injector.get(SprintService)
        self.review = injector.get(ReviewService)
        self.retro = injector.get(RetroService)
        self.orchestrator = injector.get(Orchestrator)

    def story(self, story_id: str) -> Story:
        return self.repo.get(Story, story_id)

    def plan(self, *titles: str) -> list[Story]:
        stories = [self.backlog.add_story(P, t, acceptance_criteria=[f"{t} が動く"]) for t in titles]
        self.planning.new_sprint(P, "デモが動く")
        for s in stories:
            self.planning.add_story(P, s.id)
        return stories

    def start_and_run(self) -> Sprint:
        sprint = self.sprints.start(P)
        return self.orchestrator.run(sprint.id)


@pytest.fixture
def app(injector: Injector, git_repo: Path) -> App:
    a = App(injector)
    a.projects.register(P, git_repo, max_stories_per_sprint=3)
    return a


def blocked(brief: Brief, workdir: Path) -> ExecutionResult:
    (workdir / "half.txt").write_text("途中まで\n")
    return ExecutionResult(status="blocked", summary="途中", blocked_reason="DB は何を使うか")


def self_commit(brief: Brief, workdir: Path) -> ExecutionResult:
    (workdir / "self.txt").write_text("自分でコミット\n")
    git(workdir, "add", "-A")
    git(workdir, "commit", "-m", "vuoi commit")
    return ExecutionResult(status="done", summary="コミット済み")


def raising(brief: Brief, workdir: Path) -> ExecutionResult:
    raise RuntimeError("boom")


def test_full_flow(app: App, git_repo: Path, executor: FakeExecutor, notifier: RecordingNotifier) -> None:
    s1, s2 = app.plan("一覧画面", "詳細画面")
    app.planning.add_task(s1.id, "API を作る")
    spike = app.planning.add_spike(s1.id, "ページングは必要か", options=["必要", "不要"])
    app.planning.approve(P)

    with pytest.raises(DomainError, match="未解決のスパイク"):
        app.sprints.start(P)
    app.planning.answer_spike(spike.id, "必要。20 件ずつ")
    assert app.story(s1.id).decisions == ["ページングは必要か → 必要。20 件ずつ"]

    sprint = app.start_and_run()
    assert sprint.status == SprintStatus.REVIEW
    assert sprint.branch == "caucus/sprint-1"
    assert git(git_repo, "rev-parse", "--abbrev-ref", "HEAD") == "caucus/sprint-1"
    assert [b.story_id for b in executor.calls] == [s1.id, s2.id]
    assert executor.calls[0].decisions == ["ページングは必要か → 必要。20 件ずつ"]
    assert executor.calls[0].tasks == ["API を作る"]

    for s in (s1, s2):
        story = app.story(s.id)
        assert story.status == StoryStatus.DONE
        assert len(story.commits) == 1
        message = git(git_repo, "log", "-1", "--format=%B", story.commits[0])
        assert f"Caucus-Story: {s.id}" in message
    assert all(t.status == TaskStatus.DONE for t in app.repo.query(Task, story_id=s1.id))
    assert git(git_repo, "status", "--porcelain") == ""
    assert notifier.events[-1].kind == "review_ready"

    path, text = app.review.report(P)
    assert path.exists() and "一覧画面" in text

    fb = app.review.add_feedback(P, "並び順が逆\n新しい順にしてほしい", story_id=s1.id)
    app.retro.add(P, "スパイクの粒度が粗かった")
    closed = app.review.close(P)
    assert closed.status == SprintStatus.CLOSED

    backlog = app.backlog.backlog(P)
    assert [(s.title, s.source, s.source_ref) for s in backlog] == [
        ("並び順が逆", StorySource.FEEDBACK, fb.id)
    ]
    # 次のスプリントを作れる
    assert app.planning.new_sprint(P, "次").number == 2


def test_blocked_story_is_suspended_and_changes_are_preserved(
    make_injector, git_repo: Path, notifier: RecordingNotifier
) -> None:
    executor = FakeExecutor()
    app = App(make_injector(executor))
    app.projects.register(P, git_repo)
    s1, s2 = app.plan("A", "B")
    executor.scenarios[s1.id] = blocked
    app.planning.approve(P)
    sprint = app.start_and_run()

    assert sprint.status == SprintStatus.REVIEW
    story = app.story(s1.id)
    assert story.status == StoryStatus.SUSPENDED
    assert story.wip_branch == f"caucus/wip/{s1.id}"
    assert "half.txt" in git(git_repo, "ls-tree", "--name-only", story.wip_branch)
    assert not (git_repo / "half.txt").exists()  # スプリント用ブランチには持ち込まない
    assert app.story(s2.id).status == StoryStatus.DONE  # 他のストーリーは続行

    spikes = app.repo.query(Spike, story_id=s1.id)
    assert [(s.question, s.raised_by, s.status) for s in spikes] == [
        ("DB は何を使うか", SpikeOrigin.EXECUTOR, SpikeStatus.OPEN)
    ]
    assert notifier.events[0].kind == "spike_raised"

    app.review.close(P)
    returned = app.story(s1.id)
    assert (returned.status, returned.sprint_id) == (StoryStatus.BACKLOG, None)


def test_executor_may_commit_by_itself(make_injector, git_repo: Path) -> None:
    executor = FakeExecutor(default=self_commit)
    app = App(make_injector(executor))
    app.projects.register(P, git_repo)
    (s1,) = app.plan("A")
    app.planning.approve(P)
    app.start_and_run()
    story = app.story(s1.id)
    assert len(story.commits) == 1
    assert git(git_repo, "log", "-1", "--format=%s", story.commits[0]) == "vuoi commit"


def test_executor_exception_marks_story_failed(make_injector, git_repo: Path) -> None:
    executor = FakeExecutor(default=raising)
    app = App(make_injector(executor))
    app.projects.register(P, git_repo)
    (s1,) = app.plan("A")
    app.planning.approve(P)
    assert app.start_and_run().status == SprintStatus.REVIEW
    story = app.story(s1.id)
    assert story.status == StoryStatus.FAILED
    assert story.result is not None and "boom" in (story.result.error or "")


def test_proposals_are_added_to_backlog(make_injector, git_repo: Path) -> None:
    def with_proposal(brief: Brief, workdir: Path) -> ExecutionResult:
        return ExecutionResult(
            status="done", proposals=[Proposal(title="ログ出力を整理する", body="詳細", kind="debt")]
        )

    app = App(make_injector(FakeExecutor(default=with_proposal)))
    app.projects.register(P, git_repo)
    (s1,) = app.plan("A")
    app.planning.approve(P)
    app.start_and_run()
    backlog = app.backlog.backlog(P)
    assert [(s.title, s.source, s.source_ref) for s in backlog] == [
        ("ログ出力を整理する", StorySource.EXECUTOR_PROPOSAL, s1.id)
    ]


def test_resume_recovers_story_left_running(app: App, git_repo: Path, executor: FakeExecutor) -> None:
    (s1,) = app.plan("A")
    app.planning.approve(P)
    sprint = app.sprints.start(P)

    # 実行中にプロセスが落ちた状態を再現する
    story = app.story(s1.id)
    story.start_sha = git(git_repo, "rev-parse", "HEAD")
    story.status = StoryStatus.RUNNING
    app.repo.save(story)
    (git_repo / "crash.txt").write_text("書きかけ\n")

    assert app.orchestrator.run(sprint.id).status == SprintStatus.REVIEW
    story = app.story(s1.id)
    assert story.status == StoryStatus.DONE
    assert story.wip_branch == f"caucus/wip/{s1.id}"
    assert not (git_repo / "crash.txt").exists()
    assert len(executor.calls) == 1


def test_planning_rules(app: App, git_repo: Path) -> None:
    stories = [app.backlog.add_story(P, f"S{i}") for i in range(4)]
    app.planning.new_sprint(P, "ゴール")
    with pytest.raises(DomainError, match="終了していません"):
        app.planning.new_sprint(P, "二重")
    with pytest.raises(DomainError, match="1 件もない"):
        app.planning.approve(P)
    for s in stories[:3]:
        app.planning.add_story(P, s.id)
    with pytest.raises(DomainError, match="上限"):
        app.planning.add_story(P, stories[3].id)

    app.planning.approve(P)
    with pytest.raises(InvalidTransitionError):
        app.planning.add_story(P, stories[3].id)
    with pytest.raises(InvalidTransitionError):
        app.planning.add_task(stories[0].id, "後から追加")

    app.planning.reject(P, "S0 は不要")
    app.planning.remove_story(P, stories[0].id)
    assert app.story(stories[0].id).status == StoryStatus.BACKLOG
    app.planning.approve(P)

    (git_repo / "dirty.txt").write_text("x")
    with pytest.raises(DomainError, match="未コミット"):
        app.sprints.start(P)
