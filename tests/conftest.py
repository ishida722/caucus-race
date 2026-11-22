import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from injector import Binder, Injector, InstanceProvider, Module

from caucus_race.adapter.executor.fake import FakeExecutor, SingleExecutorFactory
from caucus_race.adapter.notifier.console import RecordingNotifier
from caucus_race.adapter.planner.fake import FakePlanner, SinglePlannerFactory
from caucus_race.container import AppModule
from caucus_race.port.executor import ExecutorFactory
from caucus_race.port.notifier import Notifier
from caucus_race.port.planner import PlannerFactory
from caucus_race.settings import Settings


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "tester")
    git(repo, "config", "user.email", "tester@example.com")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("target\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "init")
    return repo


class FakeAdaptersModule(Module):
    def __init__(
        self, executor: FakeExecutor, notifier: RecordingNotifier, planner: FakePlanner | None = None
    ) -> None:
        self.executor = executor
        self.notifier = notifier
        self.planner = planner

    def configure(self, binder: Binder) -> None:
        binder.bind(ExecutorFactory, to=InstanceProvider(SingleExecutorFactory(self.executor)))  # type: ignore[type-abstract]
        binder.bind(Notifier, to=InstanceProvider(self.notifier))  # type: ignore[type-abstract]
        if self.planner is not None:
            binder.bind(PlannerFactory, to=InstanceProvider(SinglePlannerFactory(self.planner)))  # type: ignore[type-abstract]


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def executor() -> FakeExecutor:
    return FakeExecutor()


@pytest.fixture
def make_injector(tmp_path: Path, notifier: RecordingNotifier) -> Callable[[FakeExecutor], Injector]:
    settings = Settings(data_dir=tmp_path / "data")

    def make(executor: FakeExecutor) -> Injector:
        return Injector([AppModule(settings), FakeAdaptersModule(executor, notifier)])

    return make


@pytest.fixture
def injector(make_injector: Callable[[FakeExecutor], Injector], executor: FakeExecutor) -> Injector:
    return make_injector(executor)
