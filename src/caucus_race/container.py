from injector import Binder, Injector, InstanceProvider, Module, singleton

from caucus_race.adapter.executor.fake import FakeExecutor
from caucus_race.adapter.executor.vuoi import VuoiConfig, VuoiExecutor
from caucus_race.adapter.notifier.channels import (
    CommandNotifier,
    DesktopNotifier,
    HistoryNotifier,
    MultiNotifier,
    SlackWebhookNotifier,
)
from caucus_race.adapter.notifier.console import ConsoleNotifier
from caucus_race.adapter.persistence.sqlite import SqliteRepository
from caucus_race.adapter.planner.claude import ClaudePlanner, ClaudePlannerConfig
from caucus_race.adapter.planner.manual import ManualPlanner
from caucus_race.adapter.workspace.git import GitWorkspace
from caucus_race.domain.entities import Project
from caucus_race.domain.errors import DomainError
from caucus_race.port.executor import Executor, ExecutorFactory
from caucus_race.port.notifier import Notifier
from caucus_race.port.planner import Planner, PlannerFactory
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
            case "vuoi":
                options = {k: v for k, v in project.executor_config.items() if k != "type"}
                return VuoiExecutor(VuoiConfig.model_validate(options))
            case _:
                raise DomainError(f"実行器 {kind} には対応していません")


class ConfiguredPlannerFactory:
    """`Project.planner_config["type"]` に応じてプランナーを選ぶ。"""

    def for_project(self, project: Project) -> Planner:
        kind = project.planner_config.get("type", "manual")
        match kind:
            case "manual":
                return ManualPlanner()
            case "claude":
                options = {k: v for k, v in project.planner_config.items() if k != "type"}
                return ClaudePlanner(project.repo_path, ClaudePlannerConfig.model_validate(options))
            case _:
                raise DomainError(f"プランナー {kind} には対応していません")


def build_notifier(settings: Settings) -> Notifier:
    """config.toml の [notify] に従って通知の送り先を組み立てる。標準エラーと履歴には常に出す。"""
    notifiers: list[Notifier] = [
        ConsoleNotifier(),
        HistoryNotifier(settings.data_dir / "notifications.jsonl"),
    ]
    config = settings.notify
    if config.desktop:
        notifiers.append(DesktopNotifier())
    if config.command:
        notifiers.append(CommandNotifier(config.command))
    if config.slack_webhook_env:
        notifiers.append(SlackWebhookNotifier(config.slack_webhook_env))
    return MultiNotifier(notifiers)


class AppModule(Module):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def configure(self, binder: Binder) -> None:
        binder.bind(Settings, to=InstanceProvider(self.settings))
        binder.bind(Repository, to=InstanceProvider(SqliteRepository(self.settings.db_path)))  # type: ignore[type-abstract]
        binder.bind(Workspace, to=GitWorkspace, scope=singleton)  # type: ignore[type-abstract]
        binder.bind(Notifier, to=InstanceProvider(build_notifier(self.settings)))  # type: ignore[type-abstract]
        binder.bind(ExecutorFactory, to=ConfiguredExecutorFactory, scope=singleton)  # type: ignore[type-abstract]
        binder.bind(PlannerFactory, to=ConfiguredPlannerFactory, scope=singleton)  # type: ignore[type-abstract]


def build_injector(settings: Settings | None = None) -> Injector:
    return Injector([AppModule(settings or Settings.from_env())])
