"""通知の送り先。どの送り先も、失敗してもスプリントの進行を止めないよう例外を外に出さない。"""

import json
import logging
import os
import subprocess
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from caucus_race.port.notifier import HumanActionRequired, Notifier

logger = logging.getLogger(__name__)

TITLES = {
    "plan_ready": "計画の確認",
    "spike_raised": "スパイクへの回答",
    "review_ready": "スプリントレビュー",
    "runner_error": "実行の停止",
}


def title_of(event: HumanActionRequired) -> str:
    return f"caucus: {event.project} - {TITLES.get(event.kind, event.kind)}"


class DesktopNotifier:
    """macOS の通知センターに出す。"""

    def notify(self, event: HumanActionRequired) -> None:
        script = (
            f"display notification {json.dumps(event.message, ensure_ascii=False)}"
            f" with title {json.dumps(title_of(event), ensure_ascii=False)}"
        )
        try:
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10, check=True)
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("デスクトップ通知に失敗しました: %s", e)


class CommandNotifier:
    """任意のコマンドを実行し、通知の内容を JSON で標準入力に渡す。entwurf への起票などに使う。"""

    def __init__(self, command: list[str], timeout_seconds: int = 30) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds

    def notify(self, event: HumanActionRequired) -> None:
        payload = {**event.model_dump(), "title": title_of(event)}
        try:
            proc = subprocess.run(
                self.command,
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            if proc.returncode != 0:
                logger.warning("通知コマンドが失敗しました（%d）: %s", proc.returncode, proc.stderr.strip())
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("通知コマンドを実行できませんでした: %s", e)


class SlackWebhookNotifier:
    """Slack の Incoming Webhook に投稿する。URL は環境変数から読む。"""

    def __init__(self, env_name: str) -> None:
        self.env_name = env_name

    def notify(self, event: HumanActionRequired) -> None:
        url = os.environ.get(self.env_name)
        if not url:
            logger.warning("環境変数 %s が未設定のため Slack に通知できません", self.env_name)
            return
        body = json.dumps({"text": f"*{title_of(event)}*\n{event.message}"}, ensure_ascii=False).encode()
        request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10):
                pass
        except OSError as e:
            logger.warning("Slack への通知に失敗しました: %s", e)


class HistoryNotifier:
    """通知の履歴を JSON Lines で残す。バックグラウンド実行中の通知を後から確認するため。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def notify(self, event: HumanActionRequired) -> None:
        record = {"at": datetime.now(UTC).isoformat(), **event.model_dump()}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning("通知の履歴を書き込めませんでした: %s", e)

    def read(self, limit: int = 20) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines[-limit:] if line.strip()]


class MultiNotifier:
    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers

    def notify(self, event: HumanActionRequired) -> None:
        for notifier in self.notifiers:
            try:
                notifier.notify(event)
            except Exception:  # 1 つの送り先の不具合で他の送り先を止めない
                logger.exception("通知に失敗しました: %s", type(notifier).__name__)
