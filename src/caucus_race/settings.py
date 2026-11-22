import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "caucus.db"

    @classmethod
    def from_env(cls) -> Settings:
        default = Path.home() / ".local" / "share" / "caucus-race"
        return cls(data_dir=Path(os.environ.get("CAUCUS_HOME", default)).expanduser())
