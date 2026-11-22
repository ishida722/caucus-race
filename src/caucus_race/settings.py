import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class NotifyConfig:
    """`config.toml` の `[notify]`。"""

    # macOS のデスクトップ通知
    desktop: bool = False
    # 通知ごとに実行するコマンド。通知の内容を JSON で標準入力に渡す
    command: list[str] = field(default_factory=list)
    # Slack の Incoming Webhook の URL を入れた環境変数の名前（URL 自体はファイルに書かない）
    slack_webhook_env: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NotifyConfig:
        command = data.get("command", [])
        return cls(
            desktop=bool(data.get("desktop", False)),
            command=[command] if isinstance(command, str) else list(command),
            slack_webhook_env=data.get("slack_webhook_env"),
        )


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    notify: NotifyConfig = field(default_factory=NotifyConfig)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "caucus.db"

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.toml"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def run_dir(self) -> Path:
        return self.data_dir / "run"

    @classmethod
    def load(cls, data_dir: Path) -> Settings:
        """data_dir の config.toml を読む。なければ既定値を使う。"""
        config_path = data_dir / "config.toml"
        data = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        return cls(data_dir=data_dir, notify=NotifyConfig.from_dict(data.get("notify", {})))

    @classmethod
    def from_env(cls) -> Settings:
        default = Path.home() / ".local" / "share" / "caucus-race"
        return cls.load(Path(os.environ.get("CAUCUS_HOME", default)).expanduser())
