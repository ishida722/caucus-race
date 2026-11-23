"""MCP で公開する、人間の判断にあたる操作（承認・実行・レビュー）のツール。

判断するのは人間で、エージェントは人間の指示を伝える。操作は CLI と同じく人間（Actor.HUMAN）の操作として
記録される（ユースケースの既定の actor が HUMAN）。

planning_tools.py と同じく MCP の SDK には依存しない。ルール違反（DomainError など）は例外のまま投げ、
server.py がエラーの結果（isError）に変える。
"""

from collections.abc import Callable

from injector import inject
from pydantic import BaseModel, Field

from caucus_race.domain.entities import Project, Spike, Story
from caucus_race.domain.errors import DomainError
from caucus_race.domain.status import SpikeStatus, SprintStatus
from caucus_race.plan_job import PlanJobs
from caucus_race.port.repository import Repository
from caucus_race.runner import BackgroundRunner, Launched
from caucus_race.usecase.common import project_by_name, require_open_sprint, story_to_refine
from caucus_race.usecase.planning import PlanningService
from caucus_race.usecase.review import ReviewService
from caucus_race.usecase.sprint import SprintService


class OpenSpikeResult(BaseModel):
    id: str
    story_id: str
    question: str
    context: str
    options: list[str]
    recommendation: str | None
    raised_by: str


class SpikeAnswerResult(BaseModel):
    spike_id: str
    story_id: str
    decision: str = Field(description="ストーリーの決定事項に追加した内容")
    remaining_spikes: list[str] = Field(description="このストーリーに残っている未解決のスパイクの ID")
    refine_started: bool = Field(description="AI によるストーリーの見直しをバックグラウンドで始めたか")
    refine_pid: int | None = None
    refine_log_path: str | None = None
    message: str


class PlanDecisionResult(BaseModel):
    project: str
    sprint_number: int
    status: str
    message: str


class SprintRunResult(BaseModel):
    project: str
    sprint_number: int
    branch: str | None
    pid: int
    log_path: str
    message: str


class SprintStopResult(BaseModel):
    project: str
    pid: int
    message: str


class ReviewResult(BaseModel):
    project: str
    path: str = Field(description="レビュー資料（Markdown）の保存先")
    markdown: str


class FeedbackResult(BaseModel):
    id: str
    project: str
    story_id: str | None
    body: str


class ReviewCompleteResult(BaseModel):
    project: str
    sprint_number: int
    status: str
    message: str


class HumanTools:
    @inject
    def __init__(
        self,
        repo: Repository,
        planning: PlanningService,
        sprints: SprintService,
        review: ReviewService,
        runner: BackgroundRunner,
        jobs: PlanJobs,
    ) -> None:
        self.repo = repo
        self.planning = planning
        self.sprints = sprints
        self.review = review
        self.runner = runner
        self.jobs = jobs

    def _ensure_not_planning(self, project_name: str) -> None:
        """AI が計画を作成している間は、計画を承認・差し戻しさせない。

        完了時に AI の計画で置き換わるため、作成中の承認は置き換えられる計画を承認することになり、
        作成中の差し戻しコメントは黙って消える（planning_tools の計画を変更するツールと同じ扱い）。
        """
        if self.jobs.running(project_name):
            raise DomainError(
                f"{project_name} は AI がスプリント計画を作成中のため変更できません。"
                " get_plan_status で完了を確認してから操作してください"
            )

    def _launch(self, launch: Callable[[], Launched]) -> Launched:
        try:
            return launch()
        except OSError as e:
            raise DomainError(f"プロセスを起動できませんでした: {e}") from e

    # --- スパイク ---

    def list_spikes(self, project: str) -> list[OpenSpikeResult]:
        p = project_by_name(self.repo, project)
        stories = self.repo.query(Story, project_id=p.id)
        if not stories:
            return []
        spikes = self.repo.query(Spike, story_id=[s.id for s in stories], status=SpikeStatus.OPEN)
        return [
            OpenSpikeResult(
                id=s.id,
                story_id=s.story_id,
                question=s.question,
                context=s.context,
                options=s.options,
                recommendation=s.recommendation,
                raised_by=s.raised_by,
            )
            for s in spikes
        ]

    def answer_spike(self, spike_id: str, answer: str) -> SpikeAnswerResult:
        spike = self.planning.answer_spike(spike_id, answer)
        story = self.repo.get(Story, spike.story_id)
        remaining = [s.id for s in self.repo.query(Spike, story_id=story.id, status=SpikeStatus.OPEN)]
        result = SpikeAnswerResult(
            spike_id=spike.id,
            story_id=story.id,
            decision=story.decisions[-1],
            remaining_spikes=remaining,
            refine_started=False,
            message=f"{spike.id} に回答しました。回答は {story.id} の決定事項に追加しました",
        )
        target = story_to_refine(self.repo, spike)
        if target is None:
            return result
        project_name = self.repo.get(Project, target.project_id).name
        if self.jobs.running(project_name):
            # 計画ジョブと同じストーリーを並行して書き換えない。計画ジョブは最新の決定事項を使って計画を作る
            return result.model_copy(
                update={
                    "message": (
                        f"{result.message}。AI がスプリント計画を作成中のため、{target.id} の見直しは"
                        "始めませんでした（作成中の計画に回答が反映されます）"
                    )
                }
            )
        try:
            launched = self.runner.start_refine(project_name, target.id)
        except OSError as e:
            # 回答は記録済み。エラーにすると、エージェントが再試行して「回答待ちではありません」になる
            return result.model_copy(
                update={
                    "message": (
                        f"{result.message}。ただし {target.id} の見直しのプロセスを"
                        f"起動できませんでした（{e}）。"
                        f"原因を取り除いてから caucus plan refine {target.id} で見直してください"
                    )
                }
            )
        return result.model_copy(
            update={
                "refine_started": True,
                "refine_pid": launched.pid,
                "refine_log_path": str(launched.log_path),
                "message": (
                    f"{result.message}。未解決のスパイクがなくなったため、AI が {target.id} の"
                    "タスクと受け入れ条件の見直しをバックグラウンドで始めました"
                ),
            }
        )

    # --- 計画の承認・差し戻し ---

    def approve_plan(self, project: str) -> PlanDecisionResult:
        self._ensure_not_planning(project)
        sprint = self.planning.approve(project)
        return PlanDecisionResult(
            project=project,
            sprint_number=sprint.number,
            status=sprint.status,
            message=f"スプリント #{sprint.number} を承認しました。開始するには start_sprint を使う",
        )

    def reject_plan(self, project: str, comment: str) -> PlanDecisionResult:
        if not comment.strip():
            raise DomainError("差し戻しにはコメントが必要です")
        self._ensure_not_planning(project)
        sprint = self.planning.reject(project, comment)
        return PlanDecisionResult(
            project=project,
            sprint_number=sprint.number,
            status=sprint.status,
            message=f"スプリント #{sprint.number} を差し戻しました",
        )

    # --- スプリントの実行 ---

    def start_sprint(self, project: str) -> SprintRunResult:
        self.runner.ensure_not_running(project)
        sprint = self.sprints.start(project)
        try:
            launched = self.runner.start_run(project)
        except OSError as e:
            raise DomainError(
                f"スプリント #{sprint.number} は開始しましたが、実行プロセスを起動できませんでした: {e}。"
                "原因を取り除いてから resume_sprint で実行を再開してください"
            ) from e
        return SprintRunResult(
            project=project,
            sprint_number=sprint.number,
            branch=sprint.branch,
            pid=launched.pid,
            log_path=str(launched.log_path),
            message=(
                f"スプリント #{sprint.number} を開始しました（{sprint.branch}）。"
                "バックグラウンドで実行しています"
            ),
        )

    def resume_sprint(self, project: str) -> SprintRunResult:
        self.runner.ensure_not_running(project)
        sprint = require_open_sprint(self.repo, project_by_name(self.repo, project))
        if sprint.status != SprintStatus.ACTIVE:
            raise DomainError(f"スプリント #{sprint.number} は実行中ではありません（{sprint.status}）")
        launched = self._launch(lambda: self.runner.start_run(project))
        return SprintRunResult(
            project=project,
            sprint_number=sprint.number,
            branch=sprint.branch,
            pid=launched.pid,
            log_path=str(launched.log_path),
            message=f"スプリント #{sprint.number} の実行を再開しました。バックグラウンドで実行しています",
        )

    def stop_sprint(self, project: str) -> SprintStopResult:
        info = self.runner.stop_run(project)
        return SprintStopResult(
            project=project,
            pid=info.pid,
            message=(
                f"停止を指示しました（pid {info.pid}）。実行中だったストーリーは再開時に再実行されます。"
                "再開するには resume_sprint を使う"
            ),
        )

    # --- レビュー ---

    def get_review(self, project: str) -> ReviewResult:
        path, text = self.review.report(project)
        return ReviewResult(project=project, path=str(path), markdown=text)

    def add_feedback(self, project: str, body: str, story_id: str | None = None) -> FeedbackResult:
        feedback = self.review.add_feedback(project, body, story_id)
        return FeedbackResult(id=feedback.id, project=project, story_id=feedback.story_id, body=feedback.body)

    def complete_review(self, project: str) -> ReviewCompleteResult:
        sprint = self.review.close(project)
        return ReviewCompleteResult(
            project=project,
            sprint_number=sprint.number,
            status=sprint.status,
            message=(
                f"スプリント #{sprint.number} を終了しました。"
                "フィードバックはバックログのストーリーに変換し、未完了のストーリーはバックログに戻しました"
            ),
        )
