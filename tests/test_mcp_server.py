"""MCP サーバー（server.py）のテスト。MCP のクライアントからツールを呼ぶ。

- in-process: SDK の Client に MCPServer を直接渡す。ツールの登録・引数の受け渡し・エラーの結果（isError）への
  変換を、エージェントから見える形で確かめる
- stdio: `caucus mcp` を本物のプロセスとして起動し、MCP のクライアントからつなぐ（E2E）
"""

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from mcp.types import CallToolResult

from caucus_race.domain.entities import Spike, Sprint, Story
from caucus_race.mcp.server import create_server
from caucus_race.port.planner import PlannedStoryDraft, SprintPlanDraft

# フィクスチャ（env・planner）を共有するため import する（pyproject.toml で F401・F811 を除外）
from tests.test_mcp_planning import Env, GatedPlanner, P, env, planner, put_sprint_in

TOOLS = {
    "add_story": {"project", "title", "description", "acceptance_criteria"},
    "create_sprint": {"project", "goal"},
    "add_story_to_sprint": {"project", "story_id"},
    "remove_story_from_sprint": {"project", "story_id"},
    "add_task": {"story_id", "title", "description"},
    "add_spike": {"story_id", "question", "context", "options", "recommendation"},
    "plan_sprint": {"project"},
    "get_plan_status": {"project"},
    # ST-3: 人間の判断にあたる操作と、その材料の参照
    "list_spikes": {"project"},
    "answer_spike": {"spike_id", "answer"},
    "approve_plan": {"project"},
    "reject_plan": {"project", "comment"},
    "start_sprint": {"project"},
    "stop_sprint": {"project"},
    "resume_sprint": {"project"},
    "get_review": {"project"},
    "add_feedback": {"project", "body", "story_id"},
    "complete_review": {"project"},
}


def with_client[T](env: Env, body: Callable[[Client], Awaitable[T]]) -> T:
    async def run() -> T:
        async with Client(create_server(env.injector)) as client:
            return await body(client)

    return asyncio.run(run())


def call(env: Env, name: str, arguments: dict) -> CallToolResult:
    return with_client(env, lambda c: c.call_tool(name, arguments))


def text(result: CallToolResult) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


def test_server_lists_all_tools_with_their_arguments(env: Env) -> None:
    """MCP のクライアントから、README に書いたすべてのツールが、それぞれの引数とともに見えること。"""
    listed = with_client(env, lambda c: c.list_tools())
    found = {t.name: set(t.input_schema["properties"]) for t in listed.tools}
    assert found == TOOLS


@pytest.mark.parametrize(
    ("setup", "name", "arguments", "message"),
    [
        pytest.param(
            lambda env: put_sprint_in(env, "approved") and env.tools.add_story(P, "追加したい").id,
            "add_story_to_sprint",
            {"project": P, "story_id": "ST-2"},
            "スプリント #1 は approved のため計画を変更できません。 変更するには差し戻してください（R-3）",
            id="after-approval",
        ),
        pytest.param(
            lambda env: None,
            "add_story",
            {"project": "nope", "title": "一覧画面"},
            "プロジェクト nope が見つかりません",
            id="missing-project",
        ),
        pytest.param(
            lambda env: None,
            "plan_sprint",
            {"project": "manual-prj"},
            "manual-prj には AI プランナーが設定されていません"
            "（caucus project set manual-prj --planner claude）",
            id="planner-not-configured",
        ),
    ],
)
def test_server_returns_rule_violation_as_error_result_with_cli_message(
    env: Env, git_repo: Path, setup: Callable, name: str, arguments: dict, message: str
) -> None:
    """MCP のツールとして呼んでルール違反になったとき、例外ではなく isError の結果になり、
    本文が CLI の「エラー: ...」と同じメッセージ（前置きなし）であること（受け入れ条件）。"""
    env.projects.register("manual-prj", git_repo)
    setup(env)
    before = sorted((s.id, s.status, s.sprint_id) for s in env.repo.query(Story))

    result = call(env, name, arguments)

    assert result.is_error is True
    assert text(result) == message
    assert sorted((s.id, s.status, s.sprint_id) for s in env.repo.query(Story)) == before


def test_server_passes_all_arguments_to_tools(env: Env) -> None:
    """MCP のツールに渡した引数が、取り違えられずにそれぞれの項目として保存されること
    （add_story と add_spike は引数が多く、順番の取り違えが起きやすい）。"""

    async def body(c: Client) -> tuple[CallToolResult, CallToolResult]:
        story = await c.call_tool(
            "add_story",
            {
                "project": P,
                "title": "一覧",
                "description": "説明",
                "acceptance_criteria": ["条件 1", "条件 2"],
            },
        )
        spike = await c.call_tool(
            "add_spike",
            {
                "story_id": story.structured_content["id"],
                "question": "質問",
                "context": "背景",
                "options": ["案 A", "案 B"],
                "recommendation": "案 B",
            },
        )
        return story, spike

    story_result, spike_result = with_client(env, body)

    assert not story_result.is_error and not spike_result.is_error
    story = env.repo.get(Story, story_result.structured_content["id"])
    assert (story.title, story.description, story.acceptance_criteria) == (
        "一覧",
        "説明",
        ["条件 1", "条件 2"],
    )
    spike = env.repo.get(Spike, spike_result.structured_content["id"])
    assert (spike.story_id, spike.question, spike.context, spike.options, spike.recommendation) == (
        story.id,
        "質問",
        "背景",
        ["案 A", "案 B"],
        "案 B",
    )


def test_server_plan_sprint_returns_running_before_planner_finishes_then_reports_result(
    env: Env, planner: GatedPlanner
) -> None:
    """MCP のツールとして plan_sprint を呼ぶと、プランナーの完了を待たずに running が返り、
    完了後に get_plan_status で作られた計画（スプリント番号・ストーリー）が返ること（受け入れ条件の E2E）。"""
    story_id = env.tools.add_story(P, "一覧画面").id
    planner.plans.append(
        SprintPlanDraft(goal="記事を探せる", stories=[PlannedStoryDraft(story_id=story_id, title="")])
    )

    async def body(c: Client) -> tuple[dict, dict, dict]:
        started = await c.call_tool("plan_sprint", {"project": P})
        assert planner.called.wait(5)
        during = await c.call_tool("get_plan_status", {"project": P})
        planner.gate.set()
        await asyncio.to_thread(env.launcher.join)
        done = await c.call_tool("get_plan_status", {"project": P})
        return started.structured_content, during.structured_content, done.structured_content

    started, during, done = with_client(env, body)

    assert (started["status"], during["status"]) == ("running", "running")
    assert (done["status"], done["sprint_number"], done["story_ids"]) == ("succeeded", 1, [story_id])
    assert env.repo.query(Sprint)[0].goal == "記事を探せる"


def test_server_reports_unexpected_error_and_keeps_serving(env: Env) -> None:
    """想定外の例外（ルール違反ではない）が起きても、isError の結果が返ってサーバーは止まらず、
    続けて呼んだツールは動くこと。理由は get_plan_status の error で分かること。"""

    def broken(argv: list[str], log_path: Path, lock_fd: int) -> None:
        raise RuntimeError("起動器の不具合")

    env.jobs.launcher = broken

    async def body(c: Client) -> tuple[CallToolResult, CallToolResult]:
        failed = await c.call_tool("plan_sprint", {"project": P})
        status = await c.call_tool("get_plan_status", {"project": P})
        return failed, status

    failed, status = with_client(env, body)

    assert failed.is_error is True
    assert status.is_error is not True
    assert status.structured_content["status"] == "failed"
    assert "起動器の不具合" in status.structured_content["error"]


def test_caucus_mcp_command_serves_tools_over_stdio(tmp_path: Path, git_repo: Path) -> None:
    """`caucus mcp` を本物のプロセスとして起動すると、stdio で MCP のクライアントとつながり、
    ツールの一覧を返し、ストーリーを起票できること（E2E）。"""
    home = tmp_path / "home"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "caucus_race.cli.main", "mcp"],
        env={"CAUCUS_HOME": str(home), "PATH": os.environ.get("PATH", "")},
    )

    async def run() -> tuple[set[str], CallToolResult, CallToolResult]:
        async with Client(params) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            missing = await c.call_tool("add_story", {"project": P, "title": "一覧画面"})
            status = await c.call_tool("get_plan_status", {"project": P})
            return names, missing, status

    names, missing, status = asyncio.run(run())

    assert names == set(TOOLS)
    # プロジェクトは未登録なので、ルール違反の結果が返る（サーバーの DB は CAUCUS_HOME の下にある）
    assert missing.is_error is True
    assert text(missing) == f"プロジェクト {P} が見つかりません"
    assert status.is_error is True
