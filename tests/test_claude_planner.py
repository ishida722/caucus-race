import json
import sys
from pathlib import Path

import pytest

from caucus_race.adapter.planner.claude import ClaudePlanner, ClaudePlannerConfig
from caucus_race.adapter.planner.prompts import DEFAULT_SPIKE_POLICY
from caucus_race.port.planner import FeedbackView, PlannerError, PlanningContext, StoryView

# claude -p の代わりに動く偽スクリプト。FAKE_CLAUDE_OUTPUTS の出力を 1 回ごとに順に返す
FAKE_CLAUDE = """
import json, os, sys
from pathlib import Path

log = Path(os.environ["FAKE_CLAUDE_LOG"])
calls = json.loads(log.read_text()) if log.exists() else []
calls.append({"args": sys.argv[1:], "stdin": sys.stdin.read(), "cwd": os.getcwd()})
log.write_text(json.dumps(calls))
outputs = json.loads(os.environ["FAKE_CLAUDE_OUTPUTS"])
out = outputs[min(len(calls), len(outputs)) - 1]
if out == "FAIL":
    print("boom", file=sys.stderr)
    sys.exit(1)
if out == "NOT_LOGGED_IN":
    print(json.dumps({"is_error": True, "result": "Not logged in"}))
    sys.exit(1)
print(out)
"""

PLAN = {
    "goal": "記事を探せる",
    "stories": [{"story_id": "ST-1", "title": "", "tasks": [{"title": "API"}], "spikes": []}],
}


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    script = tmp_path / "fake-claude"
    script.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}")
    script.chmod(0o755)
    log = tmp_path / "calls.json"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))

    def configure(*outputs: object) -> tuple[ClaudePlanner, Path]:
        encoded = [o if isinstance(o, str) else json.dumps(o, ensure_ascii=False) for o in outputs]
        monkeypatch.setenv("FAKE_CLAUDE_OUTPUTS", json.dumps(encoded))
        planner = ClaudePlanner(tmp_path, ClaudePlannerConfig(command=str(script), model="opus"))
        return planner, log

    return configure


def context() -> PlanningContext:
    return PlanningContext(
        project_name="demo",
        project_description="記事管理",
        max_stories=3,
        backlog=[StoryView(id="ST-1", title="一覧画面")],
    )


def envelope(structured: object) -> dict:
    return {"type": "result", "is_error": False, "result": "", "structured_output": structured}


def test_plan_is_parsed_from_structured_output(fake_claude, tmp_path: Path) -> None:
    planner, log = fake_claude(envelope(PLAN))
    draft = planner.create_sprint_plan(context())

    assert draft.goal == "記事を探せる"
    assert draft.stories[0].story_id == "ST-1"
    (call,) = json.loads(log.read_text())
    args = call["args"]
    assert args[:3] == ["-p", "--output-format", "json"]
    assert args[args.index("--tools") + 1] == "Read,Glob,Grep"
    assert args[args.index("--model") + 1] == "opus"
    schema = json.loads(args[args.index("--json-schema") + 1])
    assert "goal" in schema["properties"]
    assert Path(call["cwd"]).resolve() == tmp_path.resolve()
    # プロンプトは標準入力で渡し、判断基準と入力データを含む
    assert DEFAULT_SPIKE_POLICY.splitlines()[0] in call["stdin"]
    assert "一覧画面" in call["stdin"]
    assert "3 件以内" in call["stdin"]


def test_result_text_is_used_when_structured_output_is_missing(fake_claude) -> None:
    planner, _ = fake_claude({"is_error": False, "result": json.dumps(PLAN)})
    assert planner.create_sprint_plan(context()).goal == "記事を探せる"


def test_retries_after_invalid_output(fake_claude) -> None:
    planner, log = fake_claude(envelope({"goal": "x"}), envelope(PLAN))  # 1 回目は stories がない
    assert planner.create_sprint_plan(context()).goal == "記事を探せる"
    assert len(json.loads(log.read_text())) == 2


def test_raises_after_max_attempts(fake_claude) -> None:
    planner, log = fake_claude("FAIL")
    with pytest.raises(PlannerError, match="boom"):
        planner.create_sprint_plan(context())
    assert len(json.loads(log.read_text())) == 2


def test_is_error_envelope_is_rejected(fake_claude) -> None:
    planner, _ = fake_claude({"is_error": True, "result": "rate limited"})
    with pytest.raises(PlannerError, match="rate limited"):
        planner.create_sprint_plan(context())


def test_custom_spike_policy_and_instructions(fake_claude, tmp_path: Path) -> None:
    planner, log = fake_claude(envelope({"stories": []}))
    planner.config = planner.config.model_copy(
        update={"spike_policy": "独自の基準", "instructions": "UI は英語にする"}
    )
    planner.convert_feedback(context(), [FeedbackView(id="FB-1", body="遅い")])
    stdin = json.loads(log.read_text())[0]["stdin"]
    assert "独自の基準" in stdin and DEFAULT_SPIKE_POLICY.splitlines()[0] not in stdin
    assert "UI は英語にする" in stdin
    assert "FB-1" in stdin


def test_error_in_stdout_envelope_is_reported(fake_claude) -> None:
    planner, _ = fake_claude("NOT_LOGGED_IN")
    with pytest.raises(PlannerError, match="Not logged in"):
        planner.create_sprint_plan(context())
