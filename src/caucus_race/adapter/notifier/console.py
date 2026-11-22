import sys
from typing import TextIO

from caucus_race.port.notifier import HumanActionRequired


class ConsoleNotifier:
    def __init__(self, stream: TextIO = sys.stderr) -> None:
        self.stream = stream

    def notify(self, event: HumanActionRequired) -> None:
        print(f"[要対応:{event.kind}] {event.project}: {event.message}", file=self.stream)


class RecordingNotifier:
    """テスト用。受け取った通知を保持する。"""

    def __init__(self) -> None:
        self.events: list[HumanActionRequired] = []

    def notify(self, event: HumanActionRequired) -> None:
        self.events.append(event)
