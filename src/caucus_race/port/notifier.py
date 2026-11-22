from typing import Protocol

from pydantic import BaseModel


class HumanActionRequired(BaseModel):
    """人間の対応待ちの通知。"""

    kind: str  # "spike_raised" / "review_ready" など
    project: str
    message: str


class Notifier(Protocol):
    def notify(self, event: HumanActionRequired) -> None: ...
