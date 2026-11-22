from caucus_race.port.planner import (
    FeedbackConversionDraft,
    FeedbackView,
    PlannerError,
    PlanningContext,
    ReviewGuideDraft,
    SprintPlanDraft,
    StoryRefinementDraft,
    StoryView,
    convert_feedback_mechanically,
)


class ManualPlanner:
    """AI を使わない場合のプランナー。計画は人間が CLI で作り、フィードバックは機械的に変換する。"""

    def create_sprint_plan(self, context: PlanningContext) -> SprintPlanDraft:
        raise PlannerError("AI プランナーが設定されていません（caucus project set <name> --planner claude）")

    def refine_story(self, context: PlanningContext, story: StoryView) -> StoryRefinementDraft:
        raise PlannerError("AI プランナーが設定されていません")

    def convert_feedback(
        self, context: PlanningContext, feedback: list[FeedbackView]
    ) -> FeedbackConversionDraft:
        return convert_feedback_mechanically(feedback)

    def write_review_guide(self, context: PlanningContext) -> ReviewGuideDraft:
        raise PlannerError("AI プランナーが設定されていません")
