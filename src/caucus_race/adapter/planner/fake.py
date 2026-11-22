"""テスト用のプランナー。あらかじめ用意した下書きを返し、受け取った入力を記録する。"""

from caucus_race.domain.entities import Project
from caucus_race.port.planner import (
    FeedbackConversionDraft,
    FeedbackView,
    Planner,
    PlannerError,
    PlanningContext,
    ReviewGuideDraft,
    SprintPlanDraft,
    StoryRefinementDraft,
    StoryView,
    convert_feedback_mechanically,
)


class FakePlanner:
    def __init__(
        self,
        plans: list[SprintPlanDraft] | None = None,
        refinements: list[StoryRefinementDraft] | None = None,
        conversions: list[FeedbackConversionDraft] | None = None,
        review_guides: list[ReviewGuideDraft] | None = None,
    ) -> None:
        self.review_guides = list(review_guides or [])
        self.plans = list(plans or [])
        self.refinements = list(refinements or [])
        self.conversions = list(conversions or [])
        self.contexts: list[PlanningContext] = []
        self.refined: list[StoryView] = []

    def create_sprint_plan(self, context: PlanningContext) -> SprintPlanDraft:
        self.contexts.append(context)
        return self.plans.pop(0)

    def refine_story(self, context: PlanningContext, story: StoryView) -> StoryRefinementDraft:
        self.refined.append(story)
        return self.refinements.pop(0) if self.refinements else StoryRefinementDraft()

    def convert_feedback(
        self, context: PlanningContext, feedback: list[FeedbackView]
    ) -> FeedbackConversionDraft:
        self.contexts.append(context)
        if self.conversions:
            return self.conversions.pop(0)
        return convert_feedback_mechanically(feedback)

    def write_review_guide(self, context: PlanningContext) -> ReviewGuideDraft:
        self.contexts.append(context)
        if not self.review_guides:
            raise PlannerError("確認手順が用意されていません")
        return self.review_guides.pop(0)


class SinglePlannerFactory:
    def __init__(self, planner: Planner) -> None:
        self.planner = planner

    def for_project(self, project: Project) -> Planner:
        return self.planner
