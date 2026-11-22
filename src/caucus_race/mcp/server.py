"""caucus-race を、エージェントに公開する MCP サーバー。

- バックログの起票とスプリント計画（planning_tools.py）
- 人間の判断にあたる操作（スパイク回答・計画の承認・スプリントの実行・レビュー）を、人間の指示で
  エージェントが伝えるためのツール（human_tools.py）

stdout は MCP のプロトコル通信に使うため、ここでは print しない。ログは stderr に出る。

ツールの中身は SDK に依存しない planning_tools.py・human_tools.py にある。
"""

import functools
import logging
from collections.abc import Awaitable, Callable

from injector import Injector
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

from caucus_race.container import build_injector
from caucus_race.mcp.human_tools import (
    FeedbackResult,
    HumanTools,
    OpenSpikeResult,
    PlanDecisionResult,
    ReviewCompleteResult,
    ReviewResult,
    SpikeAnswerResult,
    SprintRunResult,
    SprintStopResult,
)
from caucus_race.mcp.planning_tools import (
    PlanJobResult,
    PlanningTools,
    SpikeResult,
    SprintResult,
    StoryResult,
    TaskResult,
    ToolFailure,
    call_tool,
)

SERVER_NAME = "caucus-race"

INSTRUCTIONS = """\
caucus-race（AI エージェントによるスプリント開発のオーケストレーター）のバックログとスプリント計画を操作する。
ストーリーの起票、スプリントの作成、ストーリーの追加・除外、タスクとスパイクの追加、
AI プランナーによる計画の作成ができる。
plan_sprint は時間がかかるため、開始だけしてすぐに返る。結果は get_plan_status で確認する。

スパイクへの回答・計画の承認と差し戻し・スプリントの開始と停止と再開・フィードバックの登録・レビューの完了は
人間の判断にあたる。人間の明示的な指示があるときだけ呼ぶ。エージェントが自分の判断で呼ばない。
判断の材料には list_spikes・get_review を使う。
スプリントの開始と再開はバックグラウンドで実行し、すぐに pid とログの保存先を返す。"""

logger = logging.getLogger(__name__)


def as_tool[**P, R](fn: Callable[P, R]) -> Callable[P, Awaitable[R | CallToolResult]]:
    """ツールの関数を MCP のツールとして呼べる形にする。

    - ルール違反（DomainError など）を、エージェントに見せるエラーの結果（isError 付き）に変換する。
      SDK の ToolError を投げると「Error executing tool <name>: 」が前に付くため、結果を直接返す。
    - async 関数にして、イベントループ上で 1 つずつ実行させる。同期関数のままだと SDK が
      ワーカースレッドで並行に実行し、スレッドセーフでない Repository を同時に使ってしまう。
      どのツールも SQLite の読み書きかプロセスの起動だけで、短時間に終わる。
    """

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | CallToolResult:
        result = call_tool(lambda: fn(*args, **kwargs))
        if isinstance(result, ToolFailure):
            logger.info("ツール %s がエラーを返しました: %s", fn.__name__, result.message)
            return CallToolResult(content=[TextContent(type="text", text=result.message)], is_error=True)
        return result

    return wrapper


def create_server(injector: Injector | None = None) -> MCPServer:
    container = injector or build_injector()

    def tools() -> PlanningTools:
        return container.get(PlanningTools)

    def human() -> HumanTools:
        return container.get(HumanTools)

    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS)

    def tool[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
        server.tool()(as_tool(fn))
        return fn

    @tool
    def add_story(
        project: str, title: str, description: str = "", acceptance_criteria: list[str] | None = None
    ) -> StoryResult:
        """バックログにストーリーを追加する（caucus story add 相当）。

        Args:
            project: プロジェクト名
            title: ストーリーのタイトル（利用者にとっての価値が分かる一文）
            description: 背景や詳細
            acceptance_criteria: 受け入れ条件（1 件 1 文）
        """
        return tools().add_story(project, title, description, acceptance_criteria)

    @tool
    def create_sprint(project: str, goal: str) -> SprintResult:
        """スプリントを作成する（caucus sprint new 相当）。終了していないスプリントがあれば作成できない。

        Args:
            project: プロジェクト名
            goal: スプリントのゴール
        """
        return tools().create_sprint(project, goal)

    @tool
    def add_story_to_sprint(project: str, story_id: str) -> SprintResult:
        """バックログのストーリーを計画中（planning）のスプリントに追加する（caucus sprint add 相当）。

        Args:
            project: プロジェクト名
            story_id: ストーリーの ID（例: ST-1）
        """
        return tools().add_story_to_sprint(project, story_id)

    @tool
    def remove_story_from_sprint(project: str, story_id: str) -> SprintResult:
        """ストーリーを計画中のスプリントから外し、バックログに戻す（caucus sprint remove 相当）。

        Args:
            project: プロジェクト名
            story_id: ストーリーの ID
        """
        return tools().remove_story_from_sprint(project, story_id)

    @tool
    def add_task(story_id: str, title: str, description: str = "") -> TaskResult:
        """バックログまたは計画中のスプリントのストーリーにタスクを追加する（caucus task add 相当）。

        Args:
            story_id: ストーリーの ID
            title: タスクのタイトル
            description: 詳細
        """
        return tools().add_task(story_id, title, description)

    @tool
    def add_spike(
        story_id: str,
        question: str,
        context: str = "",
        options: list[str] | None = None,
        recommendation: str | None = None,
    ) -> SpikeResult:
        """ストーリーにスパイク（人間に回答してもらう質問）を起票する（caucus spike add 相当）。

        Args:
            story_id: ストーリーの ID
            question: 質問
            context: 質問の背景
            options: 選択肢
            recommendation: 推奨する選択肢
        """
        return tools().add_spike(story_id, question, context, options, recommendation)

    @tool
    def plan_sprint(project: str) -> PlanJobResult:
        """AI プランナーにスプリント計画を作らせる（caucus plan sprint 相当）。

        計画の作成には数分かかるため、別プロセスで開始してすぐに返る（status: running）。
        結果は get_plan_status で確認する。差し戻された計画があれば、コメントに従って作り直させる。
        作成中は、そのプロジェクトのスプリント計画を変更するツールはエラーになる。

        Args:
            project: プロジェクト名
        """
        return tools().plan_sprint(project)

    @tool
    def get_plan_status(project: str) -> PlanJobResult:
        """plan_sprint で始めた計画の作成の状態を返す。

        status は not_started / running / succeeded / failed。succeeded なら作成したスプリントの番号・ゴール・
        ストーリーの ID を、failed なら理由（error）を含む。

        Args:
            project: プロジェクト名
        """
        return tools().get_plan_status(project)

    # --- 人間の判断にあたる操作 ---

    @tool
    def list_spikes(project: str) -> list[OpenSpikeResult]:
        """プロジェクトの未解決のスパイク（人間に回答してもらう質問）を一覧する（caucus spike list 相当）。

        人間に回答を求めるときの材料にする。

        Args:
            project: プロジェクト名
        """
        return human().list_spikes(project)

    @tool
    def answer_spike(spike_id: str, answer: str) -> SpikeAnswerResult:
        """スパイクに人間の回答を登録する（caucus spike answer 相当）。

        回答はストーリーの決定事項に追加される。

        人間の明示的な指示があるときだけ呼ぶ。エージェントが自分の判断で呼ばない。
        回答は人間が述べた内容をそのまま渡す。

        AI プランナーが設定されていて、ストーリーの未解決のスパイクがなくなった場合は、
        AI によるストーリーの見直し（caucus plan refine 相当）をバックグラウンドで始め、
        完了を待たずに返る（refine_started: true）。

        Args:
            spike_id: スパイクの ID（例: SP-1）
            answer: 人間の回答
        """
        return human().answer_spike(spike_id, answer)

    @tool
    def approve_plan(project: str) -> PlanDecisionResult:
        """計画中のスプリントを承認する（caucus sprint approve 相当）。

        人間の明示的な指示があるときだけ呼ぶ。エージェントが自分の判断で呼ばない。

        Args:
            project: プロジェクト名
        """
        return human().approve_plan(project)

    @tool
    def reject_plan(project: str, comment: str) -> PlanDecisionResult:
        """スプリント計画を差し戻す（caucus sprint reject 相当）。コメントは必須。

        人間の明示的な指示があるときだけ呼ぶ。エージェントが自分の判断で呼ばない。

        Args:
            project: プロジェクト名
            comment: 差し戻しの理由（人間が述べた内容）。plan_sprint で作り直すときに AI に渡る
        """
        return human().reject_plan(project, comment)

    @tool
    def start_sprint(project: str) -> SprintRunResult:
        """承認済みのスプリントを開始する（caucus sprint start --detach 相当）。

        人間の明示的な指示があるときだけ呼ぶ。エージェントが自分の判断で呼ばない。

        スプリント用のブランチを作り、ストーリーの実行をバックグラウンドで始めて、完了を待たずに
        スプリント番号・ブランチ・pid・ログの保存先を返す。既に実行中ならエラーになる。

        Args:
            project: プロジェクト名
        """
        return human().start_sprint(project)

    @tool
    def stop_sprint(project: str) -> SprintStopResult:
        """バックグラウンドで実行中のスプリントを止める（caucus sprint stop 相当）。

        人間の明示的な指示があるときだけ呼ぶ。エージェントが自分の判断で呼ばない。

        実行中だったストーリーは resume_sprint で再開したときに再実行される。実行中でなければエラーになる。

        Args:
            project: プロジェクト名
        """
        return human().stop_sprint(project)

    @tool
    def resume_sprint(project: str) -> SprintRunResult:
        """停止したスプリントの実行を再開する（caucus sprint resume --detach 相当）。

        人間の明示的な指示があるときだけ呼ぶ。エージェントが自分の判断で呼ばない。

        バックグラウンドで実行し、完了を待たずに pid とログの保存先を返す。既に実行中ならエラーになる。

        Args:
            project: プロジェクト名
        """
        return human().resume_sprint(project)

    @tool
    def get_review(project: str) -> ReviewResult:
        """レビュー待ちのスプリントのレビュー資料を生成し、Markdown 本文と保存先を返す。

        caucus review show 相当。

        AI プランナーが設定されていると、初回は AI に確認手順を書かせるため数分かかることがある
        （2 回目以降は保存した確認手順を使う）。

        Args:
            project: プロジェクト名
        """
        return human().get_review(project)

    @tool
    def add_feedback(project: str, body: str, story_id: str | None = None) -> FeedbackResult:
        """レビュー中のスプリントに人間のフィードバックを登録する（caucus feedback add 相当）。

        人間の明示的な指示があるときだけ呼ぶ。エージェントが自分の判断で呼ばない。
        内容は人間が述べたものを渡す。

        Args:
            project: プロジェクト名
            body: フィードバックの本文
            story_id: 対象のストーリーの ID（任意）
        """
        return human().add_feedback(project, body, story_id)

    @tool
    def complete_review(project: str) -> ReviewCompleteResult:
        """レビューを完了し、スプリントを終了する（caucus review done 相当）。

        人間の明示的な指示があるときだけ呼ぶ。エージェントが自分の判断で呼ばない。

        未変換のフィードバックはストーリーに変換し（AI プランナーが設定されていれば AI が変換するため
        時間がかかることがある）、未完了のストーリーはバックログに戻す。

        Args:
            project: プロジェクト名
        """
        return human().complete_review(project)

    return server
