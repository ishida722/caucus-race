"""che vuoi（`vuoi exec`）を呼び出す実行器。

`vuoi exec <workflow> --brief <file> --json <result>` を対象リポジトリで実行し、結果 JSON を取り込む。
vuoi exec の終了コードは done=0 / blocked=2 / error=1 だが、判定には JSON の `status` を使う。
"""

import json
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from caucus_race.domain.entities import ExecutionResult, Proposal
from caucus_race.port.executor import Brief

logger = logging.getLogger(__name__)

OUTPUT_TAIL_CHARS = 2000


class VuoiConfig(BaseModel):
    """`Project.executor_config` のうち vuoi 用の設定。"""

    workflow: str = "dev"
    test_commands: list[str] = Field(default_factory=list)
    command: str = "vuoi"
    timeout_seconds: int = 3 * 60 * 60


def _tail(text: str) -> str:
    return text[-OUTPUT_TAIL_CHARS:].strip()


def _git_dir(workdir: Path) -> Path:
    """ブリーフ等の置き場所。作業ツリーに置くと変更ファイルとして混ざるので git ディレクトリ配下にする。"""
    out = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "--absolute-git-dir"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return Path(out)


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON がオブジェクトではありません")
    return data


def parse_outcome(data: dict[str, Any]) -> ExecutionResult:
    """vuoi exec の ExecOutcome（JSON）を ExecutionResult に変換する。"""
    status = data.get("status")
    if status not in ("done", "blocked", "error"):
        return ExecutionResult(status="error", error=f"vuoi の status が不正です: {status!r}", raw=data)
    proposals = [
        Proposal(title=p["title"], body=p.get("body", ""), kind=p.get("kind", "chore"))
        for p in data.get("proposals", [])
        if isinstance(p, dict) and p.get("title")
    ]
    return ExecutionResult(
        status=status,
        summary=data.get("summary", ""),
        blocked_reason=data.get("blocked") or None,
        error=data.get("error") or None,
        changed_files=list(data.get("changed_files", [])),
        proposals=proposals,
        raw=data,
    )


class VuoiExecutor:
    def __init__(self, config: VuoiConfig) -> None:
        self.config = config

    def command(self, brief_path: Path, result_path: Path) -> list[str]:
        cmd = [
            self.config.command,
            "exec",
            self.config.workflow,
            "--brief",
            str(brief_path),
            "--json",
            str(result_path),
        ]
        for test_command in self.config.test_commands:
            cmd += ["--test-command", test_command]
        return cmd

    def run(self, brief: Brief, workdir: Path) -> ExecutionResult:
        run_dir = _git_dir(workdir) / "caucus"
        run_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        base = f"{brief.story_id}-{stamp}"
        brief_path = run_dir / f"brief-{base}.md"
        result_path = run_dir / f"result-{base}.json"
        log_path = run_dir / f"log-{base}.txt"
        brief_path.write_text(brief.to_markdown(), encoding="utf-8")

        cmd = self.command(brief_path, result_path)
        logger.info("vuoi を起動します: %s（作業ツリー: %s）", " ".join(cmd), workdir)
        try:
            proc = subprocess.run(
                cmd,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )
        except FileNotFoundError:
            return ExecutionResult(status="error", error=f"{self.config.command} が見つかりません")
        except subprocess.TimeoutExpired as e:
            return ExecutionResult(
                status="error",
                error=f"vuoi が {self.config.timeout_seconds} 秒以内に終了しませんでした",
                raw={"stdout": _tail(str(e.stdout or "")), "stderr": _tail(str(e.stderr or ""))},
            )
        log_path.write_text(
            f"$ {' '.join(cmd)}\n\n{proc.stdout}\n--- stderr ---\n{proc.stderr}", encoding="utf-8"
        )

        try:
            data = _read_json(result_path)
        except (OSError, ValueError) as e:
            return ExecutionResult(
                status="error",
                error=f"vuoi の結果 JSON を読めません（終了コード {proc.returncode}）: {e}",
                raw={"stderr": _tail(proc.stderr), "log": str(log_path)},
            )
        result = parse_outcome(data)
        result.raw["log"] = str(log_path)
        return result
