from pathlib import Path

import pytest
from injector import Injector

from caucus_race.adapter.executor.fake import FakeExecutor
from caucus_race.adapter.notifier.console import RecordingNotifier
from caucus_race.adapter.planner.fake import FakePlanner
from caucus_race.container import AppModule
from caucus_race.domain.entities import Feedback, Spike, Sprint, Task
from caucus_race.domain.status import SpikeStatus, SprintStatus, StorySource, StoryStatus
from caucus_race.port.planner import (
    FeedbackConversionDraft,
    FeedbackStoryDraft,
    PlannedStoryDraft,
    PlannerError,
    SpikeDraft,
    SprintPlanDraft,
    StoryRefinementDraft,
    TaskDraft,
)
from caucus_race.settings import Settings
from caucus_race.usecase.ai_planning import AIPlanningService
from tests.conftest import FakeAdaptersModule
from tests.test_sprint_flow import App, P


class AIApp(App):
    def __init__(self, injector: Injector) -> None:
        super().__init__(injector)
        self.ai = injector.get(AIPlanningService)


@pytest.fixture
def planner() -> FakePlanner:
    return FakePlanner()


@pytest.fixture
def ai(tmp_path: Path, git_repo: Path, planner: FakePlanner, notifier: RecordingNotifier) -> AIApp:
    injector = Injector(
        [
            AppModule(Settings(data_dir=tmp_path / "data")),
            FakeAdaptersModule(FakeExecutor(), notifier, planner),
        ]
    )
    app = AIApp(injector)
    app.projects.register(P, git_repo, description="記事管理アプリ", max_stories_per_sprint=2)
    return app


def plan_draft(*stories: PlannedStoryDraft, goal: str = "記事を探せる") -> SprintPlanDraft:
    return SprintPlanDraft(goal=goal, stories=list(stories), rationale="理由")


def test_plan_creates_sprint_from_draft(ai: AIApp, planner: FakePlanner, notifier: RecordingNotifier) -> None:
    s1 = ai.backlog.add_story(P, "一覧画面")
    ai.backlog.add_story(P, "検索")
    planner.plans.append(
        plan_draft(
            PlannedStoryDraft(
                story_id=s1.id,
                title="",
                acceptance_criteria=["新しい順に並ぶ"],
                tasks=[TaskDraft(title="API"), TaskDraft(title="画面")],
                spikes=[
                    SpikeDraft(question="ページングは必要か", options=["必要", "不要"], recommendation="必要")
                ],
            ),
            PlannedStoryDraft(title="記事の削除", tasks=[TaskDraft(title="削除 API")]),
        )
    )

    sprint = ai.ai.plan(P)

    context = planner.contexts[0]
    assert context.project_description == "記事管理アプリ"
    assert context.max_stories == 2
    assert [s.title for s in context.backlog] == ["一覧画面", "検索"]

    assert (sprint.number, sprint.goal, sprint.status) == (1, "記事を探せる", SprintStatus.PLANNING)
    story = ai.story(s1.id)
    assert (story.status, story.sprint_id) == (StoryStatus.PLANNED, sprint.id)
    assert story.acceptance_criteria == ["新しい順に並ぶ"]
    assert [t.title for t in ai.repo.query(Task, story_id=s1.id)] == ["API", "画面"]
    (spike,) = ai.repo.query(Spike, story_id=s1.id)
    assert (spike.question, spike.options, spike.recommendation) == (
        "ページングは必要か",
        ["必要", "不要"],
        "必要",
    )

    new = [s for s in ai.repo.query(type(story), sprint_id=sprint.id) if s.id != s1.id]
    assert [(s.title, s.source) for s in new] == [("記事の削除", StorySource.AI)]
    assert notifier.events[-1].kind == "plan_ready"
    assert "スパイク 1 件" in notifier.events[-1].message


def test_plan_ignores_invalid_stories_and_truncates(ai: AIApp, planner: FakePlanner) -> None:
    s1 = ai.backlog.add_story(P, "A")
    planner.plans.append(
        plan_draft(
            PlannedStoryDraft(story_id="ST-999", title="存在しない"),
            PlannedStoryDraft(story_id=s1.id, title=""),
            PlannedStoryDraft(title="新規 B"),
        )
    )
    sprint = ai.ai.plan(P)
    # 上限 2 件で切ったうえで、存在しない ID は無視する
    assert [s.title for s in ai.repo.query(type(ai.story(s1.id)), sprint_id=sprint.id)] == ["A"]


def test_plan_without_usable_stories_rolls_back(ai: AIApp, planner: FakePlanner) -> None:
    planner.plans.append(plan_draft(PlannedStoryDraft(story_id="ST-999", title="")))
    with pytest.raises(PlannerError):
        ai.ai.plan(P)
    assert ai.repo.query(Sprint) == []


def test_replan_after_reject(ai: AIApp, planner: FakePlanner) -> None:
    s1 = ai.backlog.add_story(P, "A")
    s2 = ai.backlog.add_story(P, "B")
    planner.plans.append(
        plan_draft(
            PlannedStoryDraft(
                story_id=s1.id,
                title="",
                spikes=[SpikeDraft(question="Q1"), SpikeDraft(question="Q2")],
            )
        )
    )
    ai.ai.plan(P)
    q1, q2 = ai.repo.query(Spike, story_id=s1.id)
    ai.planning.answer_spike(q1.id, "はい")
    ai.planning.reject(P, "B を先にやってほしい")

    planner.plans.append(plan_draft(PlannedStoryDraft(story_id=s2.id, title=""), goal="B が動く"))
    sprint = ai.ai.plan(P)

    context = planner.contexts[-1]
    assert context.reject_comment == "B を先にやってほしい"
    assert context.current_plan is not None and [s.id for s in context.current_plan.stories] == [s1.id]
    assert (sprint.number, sprint.goal, sprint.plan_comment) == (1, "B が動く", None)
    assert ai.story(s1.id).status == StoryStatus.BACKLOG
    assert ai.story(s1.id).decisions == ["Q1 → はい"]  # 回答は残る
    assert ai.repo.get(Spike, q2.id).status == SpikeStatus.DROPPED
    assert ai.story(s2.id).status == StoryStatus.PLANNED


def test_refine_replaces_tasks_and_adds_spikes(ai: AIApp, planner: FakePlanner) -> None:
    s1 = ai.backlog.add_story(P, "A")
    planner.plans.append(
        plan_draft(
            PlannedStoryDraft(
                story_id=s1.id,
                title="",
                tasks=[TaskDraft(title="旧")],
                spikes=[SpikeDraft(question="DB は？")],
            )
        )
    )
    ai.ai.plan(P)
    (spike,) = ai.repo.query(Spike, story_id=s1.id)
    ai.planning.answer_spike(spike.id, "SQLite")
    ai.planning.approve(P)  # 承認後でも見直せる

    planner.refinements.append(
        StoryRefinementDraft(
            acceptance_criteria=["SQLite に保存される"],
            tasks=[TaskDraft(title="スキーマ"), TaskDraft(title="保存処理")],
            spikes=[SpikeDraft(question="既存データは移行するか")],
        )
    )
    ai.ai.refine(s1.id)

    assert planner.refined[0].decisions == ["DB は？ → SQLite"]
    assert ai.story(s1.id).acceptance_criteria == ["SQLite に保存される"]
    assert [t.title for t in ai.repo.query(Task, story_id=s1.id)] == ["スキーマ", "保存処理"]
    open_spikes = ai.repo.query(Spike, story_id=s1.id, status=SpikeStatus.OPEN)
    assert [s.question for s in open_spikes] == ["既存データは移行するか"]


def review_ready(ai: AIApp, planner: FakePlanner) -> str:
    s1 = ai.backlog.add_story(P, "A")
    planner.plans.append(plan_draft(PlannedStoryDraft(story_id=s1.id, title="")))
    ai.ai.plan(P)
    ai.planning.approve(P)
    ai.start_and_run()
    return s1.id


def test_feedback_is_converted_by_ai_on_close(ai: AIApp, planner: FakePlanner) -> None:
    story_id = review_ready(ai, planner)
    fb1 = ai.review.add_feedback(P, "並び順が逆", story_id=story_id)
    fb2 = ai.review.add_feedback(P, "新しい順にしてほしい")
    fb3 = ai.review.add_feedback(P, "件数を表示したい")
    planner.conversions.append(
        FeedbackConversionDraft(
            stories=[
                FeedbackStoryDraft(
                    feedback_ids=[fb1.id, fb2.id],
                    title="一覧を新しい順にする",
                    acceptance_criteria=["新しい順"],
                )
            ]
        )
    )
    ai.review.close(P)

    backlog = ai.backlog.backlog(P)
    assert [(s.title, s.source_ref) for s in backlog] == [
        ("一覧を新しい順にする", f"{fb1.id},{fb2.id}"),
        ("件数を表示したい", fb3.id),  # AI が取りこぼした分は機械的に変換される
    ]
    assert all(f.converted_story_id for f in ai.repo.query(Feedback))


def test_next_plan_sees_previous_sprint(ai: AIApp, planner: FakePlanner) -> None:
    review_ready(ai, planner)
    ai.review.add_feedback(P, "件数を表示したい")
    ai.retro.add(P, "スパイクが粗かった")
    ai.review.close(P)

    planner.plans.append(plan_draft(PlannedStoryDraft(title="次")))
    ai.ai.plan(P)
    previous = planner.contexts[-1].previous_sprint
    assert previous is not None
    assert previous.feedback == ["件数を表示したい"]
    assert previous.retro_notes == ["スパイクが粗かった"]
    assert [s.status for s in previous.stories] == ["done"]
