"""人間の判断にあたる操作のツールを、MCP のクライアントから成功させるテスト（ST-3）。

- in-process: SDK の Client に MCPServer を直接渡し、
  各ツールの登録（どのツールがどの処理につながるか）・引数の受け渡し・
  構造化された返り値（pid・ログの保存先・Markdown など）を、エージェントから見える形で確かめる
- stdio（E2E）: `caucus mcp` を本物のプロセスとして起動し、MCP のクライアントからスプリントを開始して、
  本物の実行プロセス（`caucus sprint run`）がレビュー待ちまで進め、レビューを完了するまでを確かめる

in-process のテストでモックするのはプロセスの起動（spawn）だけ。停止は、
ロックを持つ本物のプロセスを止めて確かめる。
"""

import asyncio
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, StdioServerParameters
from mcp.types import CallToolResult
from typer.testing import CliRunner

from caucus_race.cli import main as cli
from caucus_race.domain.entities import Feedback, Sprint, Story
from caucus_race.domain.status import SprintStatus
from caucus_race.mcp.server import create_server
from caucus_race.orchestrator import Orchestrator
from caucus_race.runner import RunLock
from tests.test_mcp_human_tools import Env, P


@pytest.fixture
def env(tmp_path: Path, git_repo: Path) -> Env:
    return Env(tmp_path, git_repo)


def with_client[T](env: Env, body: Callable[[Client], Awaitable[T]]) -> T:
    async def run() -> T:
        async with Client(create_server(env.injector)) as client:
            return await body(client)

    return asyncio.run(run())


def ok(result: CallToolResult) -> dict[str, Any]:
    """成功の結果であることを確かめ、構造化された返り値を返す。"""
    assert result.is_error is not True, "".join(getattr(c, "text", "") for c in result.content)
    assert result.structured_content is not None
    return result.structured_content


def test_tools_called_through_mcp_reach_their_own_operation_with_all_arguments(env: Env) -> None:
    """MCP のクライアントから、スパイクの一覧 → 回答 → 差し戻し → 承認 → 開始 → 再開 →（実行の完了）→
    レビュー資料の取得 → フィードバック（ストーリーの指定あり）→ レビュー完了 の順に呼んだとき、
    各ツールがそれぞれの処理を行い（別のツールの処理に入れ替わっていない）、渡した引数がそのまま使われ、
    構造化された返り値に、
    スプリント番号・ブランチ・pid・ログの保存先・Markdown 本文・保存先などが入ること。"""
    story = env.backlog.add_story(P, "一覧画面")
    env.planning.new_sprint(P, "一覧が見られる")
    env.planning.add_story(P, story.id)
    spike = env.planning.add_spike(story.id, "並び順は？")

    async def body(c: Client) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        out["list"] = ok(await c.call_tool("list_spikes", {"project": P}))
        out["answer"] = ok(await c.call_tool("answer_spike", {"spike_id": spike.id, "answer": "新しい順"}))
        out["reject"] = ok(await c.call_tool("reject_plan", {"project": P, "comment": "件数も見たい"}))
        out["approve"] = ok(await c.call_tool("approve_plan", {"project": P}))
        out["start"] = ok(await c.call_tool("start_sprint", {"project": P}))
        out["resume"] = ok(await c.call_tool("resume_sprint", {"project": P}))
        sprint = env.repo.query(Sprint)[0]
        env.injector.get(Orchestrator).run(
            sprint.id
        )  # 起動した実行プロセスの代わりに、この場で最後まで実行する
        out["review"] = ok(await c.call_tool("get_review", {"project": P}))
        out["feedback"] = ok(
            await c.call_tool(
                "add_feedback", {"project": P, "body": "件数も表示したい", "story_id": story.id}
            )
        )
        out["complete"] = ok(await c.call_tool("complete_review", {"project": P}))
        return out

    out = with_client(env, body)

    assert [s["id"] for s in out["list"]["result"]] == [spike.id]
    assert (out["answer"]["spike_id"], out["answer"]["refine_started"]) == (spike.id, True)
    assert "新しい順" in out["answer"]["decision"]
    assert out["reject"]["status"] == SprintStatus.PLANNING
    assert out["approve"]["status"] == SprintStatus.APPROVED
    assert (out["start"]["sprint_number"], out["start"]["branch"]) == (1, "caucus/sprint-1")
    assert out["start"]["pid"] == env.spawn.pid and out["start"]["log_path"].endswith(".log")
    assert out["resume"]["pid"] == env.spawn.pid
    assert len(env.spawn.calls) == 3  # 見直し・開始・再開で 1 回ずつ起動した
    assert "スプリント #1 レビュー" in out["review"]["markdown"]
    assert Path(out["review"]["path"]).read_text(encoding="utf-8") == out["review"]["markdown"]
    [feedback] = env.repo.query(Feedback)
    assert (out["feedback"]["id"], feedback.body, feedback.story_id) == (
        feedback.id,
        "件数も表示したい",
        story.id,
    )
    assert out["complete"]["status"] == SprintStatus.CLOSED
    assert env.repo.query(Sprint)[0].status == SprintStatus.CLOSED
    assert env.repo.get(Story, story.id).decisions != []


def test_stop_sprint_through_mcp_stops_running_process_and_tells_how_to_resume(env: Env) -> None:
    """MCP のクライアントから stop_sprint を呼ぶと、実行ロックを持つ本物のプロセスが止まり、
    構造化された返り値に、そのプロセスの pid と再開方法（resume_sprint）が入ること。"""
    story = env.backlog.add_story(P, "一覧画面")
    env.planning.new_sprint(P, "一覧が見られる")
    env.planning.add_story(P, story.id)
    env.planning.approve(P)
    env.tools.start_sprint(P)
    script = (
        "import time; from pathlib import Path; from caucus_race.runner import RunLock\n"
        f"with RunLock(Path({str(env.settings.run_dir)!r}), {P!r}).hold():\n"
        "    time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        while RunLock(env.settings.run_dir, P).running() is None:
            assert time.monotonic() < deadline, "子プロセスがロックを取りませんでした"
            time.sleep(0.05)

        result = ok(with_client(env, lambda c: c.call_tool("stop_sprint", {"project": P})))

        assert proc.wait(10) != 0
        assert result["pid"] == proc.pid
        assert "resume_sprint" in result["message"]
    finally:
        if proc.poll() is None:
            proc.kill()


def test_get_review_description_says_it_may_take_time(env: Env) -> None:
    """get_review の説明に、
    確認手順の生成で AI を呼ぶ場合は時間がかかる旨が書かれていること（作業内容の指定）。"""
    listed = with_client(env, lambda c: c.list_tools())
    description = next(t.description or "" for t in listed.tools if t.name == "get_review")

    assert "AI" in description
    assert "数分かかる" in description or "時間がかかる" in description


# --- E2E: `caucus mcp` を本物のプロセスとして起動する ---

cli_runner = CliRunner()


def test_caucus_mcp_starts_real_sprint_run_that_reaches_review_and_completes(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`caucus mcp` を本物のプロセスとして起動し、MCP のクライアントから start_sprint を呼ぶと、
    完了を待たずに pid とログの保存先が返り、本物の実行プロセス（`caucus sprint run`。
    偽の実行器）がスプリントを
    レビュー待ちまで進めること。その後、
    get_review・add_feedback・complete_review でレビューを完了できること（E2E）。"""
    home = tmp_path / "home"
    monkeypatch.setenv("CAUCUS_HOME", str(home))
    cli.container.cache_clear()
    for args in (
        ["project", "add", P, "--repo", str(git_repo)],
        ["story", "add", P, "一覧画面"],
        ["sprint", "new", P, "--goal", "一覧が見られる"],
        ["sprint", "add", P, "ST-1"],
        ["sprint", "approve", P],
    ):
        result = cli_runner.invoke(cli.app, args)
        assert result.exit_code == 0, result.output
    cli.container.cache_clear()
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "caucus_race.cli.main", "mcp"],
        env={"CAUCUS_HOME": str(home), "PATH": os.environ.get("PATH", "")},
    )

    async def run() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        async with Client(params) as c:
            started = ok(await c.call_tool("start_sprint", {"project": P}))
            deadline = time.monotonic() + 60
            while True:  # 実行プロセスがレビュー待ちまで進めるのを待つ
                review = await c.call_tool("get_review", {"project": P})
                if review.is_error is not True:
                    break
                assert time.monotonic() < deadline, "スプリントがレビュー待ちになりませんでした"
                await asyncio.sleep(0.5)
            feedback = ok(await c.call_tool("add_feedback", {"project": P, "body": "件数も表示したい"}))
            completed = ok(await c.call_tool("complete_review", {"project": P}))
            return started, ok(review), feedback, completed

    started, review, feedback, completed = asyncio.run(run())

    assert started["sprint_number"] == 1 and started["pid"] > 0
    assert "一覧画面" in review["markdown"]
    assert feedback["story_id"] is None
    assert completed["status"] == SprintStatus.CLOSED
    log = Path(started["log_path"])
    assert log.exists() and "review になりました" in log.read_text(encoding="utf-8")
