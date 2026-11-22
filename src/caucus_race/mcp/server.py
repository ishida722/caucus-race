"""caucus-race のバックログ起票とスプリント計画を、エージェントに公開する MCP サーバー。

stdout は MCP のプロトコル通信に使うため、ここでは print しない。ログは stderr に出る。

ツールの中身は SDK に依存しない planning_tools.py にある。
"""

import functools
import logging
from collections.abc import Awaitable, Callable

from injector import Injector
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

from caucus_race.container import build_injector
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
計画の承認・スパイクへの回答・スプリントの実行は人間が caucus コマンドで行う。
plan_sprint は時間がかかるため、開始だけしてすぐに返る。結果は get_plan_status で確認する。"""

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

    return server
