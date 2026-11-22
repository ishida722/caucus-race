from injector import Binder, Injector, InstanceProvider, Module, singleton

from caucus_race.adapter.executor.fake import FakeExecutor
from caucus_race.adapter.notifier.console import ConsoleNotifier
from caucus_race.adapter.persistence.sqlite import SqliteRepository
from caucus_race.adapter.workspace.git import GitWorkspace
from caucus_race.domain.entities import Project
from caucus_race.domain.errors import DomainError
from caucus_race.port.executor import Executor, ExecutorFactory
from caucus_race.port.notifier import Notifier
from caucus_race.port.repository import Repository
from caucus_race.port.workspace import Workspace
from caucus_race.settings import Settings


class ConfiguredExecutorFactory:
    """`Project.executor_config["type"]` に応じて実行器を選ぶ。"""

    def for_project(self, project: Project) -> Executor:
        kind = project.executor_config.get("type", "fake")
        match kind:
            case "fake":
                return FakeExecutor()
            case _:
                raise DomainError(f"実行器 {kind} には対応していません")


class AppModule(Module):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def configure(self, binder: Binder) -> None:
        binder.bind(Settings, to=InstanceProvider(self.settings))
        binder.bind(Repository, to=InstanceProvider(SqliteRepository(self.settings.db_path)))  # type: ignore[type-abstract]
        binder.bind(Workspace, to=GitWorkspace, scope=singleton)  # type: ignore[type-abstract]
        binder.bind(Notifier, to=ConsoleNotifier, scope=singleton)  # type: ignore[type-abstract]
        binder.bind(ExecutorFactory, to=ConfiguredExecutorFactory, scope=singleton)  # type: ignore[type-abstract]


def build_injector(settings: Settings | None = None) -> Injector:
    return Injector([AppModule(settings or Settings.from_env())])
