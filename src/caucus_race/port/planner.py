"""AI プランナーの port。

プランナーは下書き（Draft）を返すだけで、DB への反映はユースケースが行う。
"""

from typing import Protocol

from pydantic import BaseModel, Field

from caucus_race.domain.entities import Project


class PlannerError(Exception):
    """プランナーを使えない、または出力が不正。"""


# --- プランナーへの入力 ---


class StoryView(BaseModel):
    id: str
    title: str
    description: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    source: str = ""
    status: str = ""
    open_spikes: list[str] = Field(default_factory=list)
    tasks: list[str] = Field(default_factory=list)
    result_summary: str = ""


class SprintView(BaseModel):
    number: int
    goal: str
    stories: list[StoryView] = Field(default_factory=list)
    feedback: list[str] = Field(default_factory=list)
    retro_notes: list[str] = Field(default_factory=list)


class PlanningContext(BaseModel):
    project_name: str
    project_description: str = ""
    max_stories: int
    backlog: list[StoryView] = Field(default_factory=list)
    previous_sprint: SprintView | None = None
    # 差し戻し後の再計画のときだけ設定する
    current_plan: SprintView | None = None
    reject_comment: str | None = None


# --- プランナーの出力 ---


class TaskDraft(BaseModel):
    title: str
    description: str = ""


class SpikeDraft(BaseModel):
    question: str = Field(description="人間に決めてほしいこと。短い返答で答えられる形にする")
    context: str = Field(default="", description="判断材料。背景や調べた内容")
    options: list[str] = Field(default_factory=list, description="選択肢")
    recommendation: str = Field(default="", description="AI の推奨案と理由")


class PlannedStoryDraft(BaseModel):
    story_id: str | None = Field(
        default=None, description="バックログの既存ストーリーの ID。新しいストーリーなら null"
    )
    title: str = Field(description="新規なら必須。既存なら空でよい（変更しない）")
    description: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    tasks: list[TaskDraft] = Field(default_factory=list)
    spikes: list[SpikeDraft] = Field(default_factory=list)


class SprintPlanDraft(BaseModel):
    goal: str = Field(description="スプリント終了時に達成されている状態を言葉で表したもの")
    stories: list[PlannedStoryDraft]
    rationale: str = Field(default="", description="この選定にした理由")


class StoryRefinementDraft(BaseModel):
    acceptance_criteria: list[str] = Field(default_factory=list)
    tasks: list[TaskDraft] = Field(default_factory=list)
    spikes: list[SpikeDraft] = Field(
        default_factory=list, description="回答を受けて新たに生じた、人間の判断が必要な事項"
    )


class FeedbackStoryDraft(BaseModel):
    feedback_ids: list[str] = Field(description="このストーリーの元になったフィードバックの ID")
    title: str
    description: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)


class FeedbackConversionDraft(BaseModel):
    stories: list[FeedbackStoryDraft]


class ReviewGuideDraft(BaseModel):
    overview: str = Field(description="このスプリントで何ができるようになったかの要約。2〜4 文")
    steps: list[str] = Field(description="人間が成果物を実際に操作して確認する手順。起動方法から具体的に書く")
    cautions: list[str] = Field(
        default_factory=list, description="確認するときの注意点、未完了・中断したストーリーの影響"
    )

    def to_markdown(self) -> str:
        lines = [self.overview, "", "確認手順:"]
        lines += [f"{i}. {s}" for i, s in enumerate(self.steps, 1)]
        if self.cautions:
            lines += ["", "注意点:"]
            lines += [f"- {c}" for c in self.cautions]
        return "\n".join(lines)


class FeedbackView(BaseModel):
    id: str
    body: str
    story: StoryView | None = None


def convert_feedback_mechanically(feedback: list[FeedbackView]) -> FeedbackConversionDraft:
    """AI を使わずに、フィードバック 1 件をストーリー 1 件に変換する。本文の 1 行目をタイトルにする。"""
    stories = []
    for fb in feedback:
        description = fb.body + (f"\n\n（{fb.story.id} へのフィードバック）" if fb.story else "")
        title = (fb.body.splitlines() or [""])[0][:200] or "（無題のフィードバック）"
        stories.append(FeedbackStoryDraft(feedback_ids=[fb.id], title=title, description=description))
    return FeedbackConversionDraft(stories=stories)


class Planner(Protocol):
    def create_sprint_plan(self, context: PlanningContext) -> SprintPlanDraft: ...

    def refine_story(self, context: PlanningContext, story: StoryView) -> StoryRefinementDraft: ...

    def convert_feedback(
        self, context: PlanningContext, feedback: list[FeedbackView]
    ) -> FeedbackConversionDraft: ...

    def write_review_guide(self, context: PlanningContext) -> ReviewGuideDraft:
        """context.current_plan のスプリントについて、人間が成果物を確認する手順を書く。"""
        ...


class PlannerFactory(Protocol):
    def for_project(self, project: Project) -> Planner: ...
