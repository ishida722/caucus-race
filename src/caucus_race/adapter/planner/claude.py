"""Claude Code をヘッドレス（`claude -p`）で呼び出すプランナー。

対象リポジトリをカレントにして読み取り専用のツールだけを渡し、`--json-schema` で構造化出力を受け取る。
"""

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from caucus_race.adapter.planner import prompts
from caucus_race.port.planner import (
    FeedbackConversionDraft,
    FeedbackView,
    PlannerError,
    PlanningContext,
    ReviewGuideDraft,
    SprintPlanDraft,
    StoryRefinementDraft,
    StoryView,
)

logger = logging.getLogger(__name__)


class ClaudePlannerConfig(BaseModel):
    """`Project.planner_config` のうち Claude 用の設定。"""

    command: str = "claude"
    model: str | None = None
    tools: list[str] = Field(default_factory=lambda: ["Read", "Glob", "Grep"])
    timeout_seconds: int = 30 * 60
    max_attempts: int = 2
    # 指定するとスパイクの判断基準を置き換える
    spike_policy: str | None = None
    # 追加の指示（プロジェクト固有の方針など）
    instructions: str = ""


def _extract(stdout: str) -> Any:
    """`--output-format json` の出力から構造化出力を取り出す。"""
    envelope = json.loads(stdout)
    if not isinstance(envelope, dict):
        raise ValueError("出力が JSON オブジェクトではありません")
    if envelope.get("is_error"):
        raise ValueError(f"claude がエラーを返しました: {envelope.get('result')}")
    if envelope.get("structured_output") is not None:
        return envelope["structured_output"]
    result = envelope.get("result")
    if isinstance(result, str):
        return json.loads(result)
    raise ValueError("構造化出力がありません")


def _error_message(stdout: str, stderr: str) -> str:
    """失敗時の理由。claude は `--output-format json` のとき、エラーも標準出力の JSON の result に書く。"""
    try:
        envelope = json.loads(stdout)
        if isinstance(envelope, dict) and envelope.get("result"):
            return str(envelope["result"])
    except ValueError:
        pass
    return (stderr.strip() or stdout.strip())[-1000:]


class ClaudePlanner:
    def __init__(self, workdir: Path, config: ClaudePlannerConfig) -> None:
        self.workdir = workdir
        self.config = config

    def _prompt(self, task: str, payload: dict[str, Any]) -> str:
        policy = self.config.spike_policy or prompts.DEFAULT_SPIKE_POLICY
        parts = [prompts.ROLE, policy]
        if self.config.instructions:
            parts.append(f"## プロジェクト固有の指示\n\n{self.config.instructions}\n")
        parts.append(task)
        parts.append("## 入力\n\n```json\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```\n")
        return "\n".join(parts)

    def command(self, schema: dict[str, Any]) -> list[str]:
        cmd = [
            self.config.command,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, ensure_ascii=False),
            "--tools",
            ",".join(self.config.tools),
            "--permission-mode",
            "dontAsk",
            "--no-session-persistence",
        ]
        if self.config.model:
            cmd += ["--model", self.config.model]
        return cmd

    def _ask[M: BaseModel](self, prompt: str, model: type[M]) -> M:
        cmd = self.command(model.model_json_schema())
        last_error = ""
        for attempt in range(1, self.config.max_attempts + 1):
            logger.info("claude に問い合わせます（%s、%d 回目）", model.__name__, attempt)
            try:
                proc = subprocess.run(
                    cmd,
                    input=prompt,
                    cwd=self.workdir,
                    capture_output=True,
                    text=True,
                    timeout=self.config.timeout_seconds,
                    check=False,
                )
            except FileNotFoundError as e:
                raise PlannerError(f"{self.config.command} が見つかりません") from e
            except subprocess.TimeoutExpired:
                last_error = f"{self.config.timeout_seconds} 秒以内に終了しませんでした"
                continue
            if proc.returncode != 0:
                last_error = f"終了コード {proc.returncode}: {_error_message(proc.stdout, proc.stderr)}"
                continue
            try:
                return model.model_validate(_extract(proc.stdout))
            except (ValueError, ValidationError) as e:
                last_error = f"出力を解釈できません: {e}"
                logger.warning("claude の出力を解釈できませんでした: %s", e)
        raise PlannerError(f"claude によるプランニングに失敗しました: {last_error}")

    def create_sprint_plan(self, context: PlanningContext) -> SprintPlanDraft:
        task = prompts.PLAN.format(max_stories=context.max_stories)
        if context.current_plan is not None:
            task += "\n" + prompts.REPLAN
        return self._ask(self._prompt(task, context.model_dump(exclude_none=True)), SprintPlanDraft)

    def refine_story(self, context: PlanningContext, story: StoryView) -> StoryRefinementDraft:
        payload = {**context.model_dump(exclude_none=True), "target_story": story.model_dump()}
        return self._ask(self._prompt(prompts.REFINE, payload), StoryRefinementDraft)

    def convert_feedback(
        self, context: PlanningContext, feedback: list[FeedbackView]
    ) -> FeedbackConversionDraft:
        payload = {
            **context.model_dump(exclude_none=True),
            "feedback": [f.model_dump(exclude_none=True) for f in feedback],
        }
        return self._ask(self._prompt(prompts.CONVERT_FEEDBACK, payload), FeedbackConversionDraft)

    def write_review_guide(self, context: PlanningContext) -> ReviewGuideDraft:
        return self._ask(
            self._prompt(prompts.REVIEW_GUIDE, context.model_dump(exclude_none=True)), ReviewGuideDraft
        )
