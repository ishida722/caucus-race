import logging
import signal
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Annotated, Any

import typer
from injector import Injector

from caucus_race.adapter.notifier.channels import HistoryNotifier
from caucus_race.container import build_injector
from caucus_race.domain.entities import Feedback, Project, Spike, Story, Task
from caucus_race.domain.errors import DomainError
from caucus_race.domain.status import Author, SpikeStatus, SprintStatus
from caucus_race.orchestrator import Orchestrator
from caucus_race.port.notifier import HumanActionRequired, Notifier
from caucus_race.port.planner import PlannerError
from caucus_race.port.repository import Repository
from caucus_race.port.workspace import WorkspaceError
from caucus_race.runner import RunLock, runner_argv, spawn_detached
from caucus_race.runner import stop as stop_run
from caucus_race.settings import Settings
from caucus_race.usecase.ai_planning import AIPlanningService
from caucus_race.usecase.common import (
    latest_sprint,
    open_spikes,
    project_by_name,
    require_open_sprint,
    sprint_stories,
)
from caucus_race.usecase.planning import PlanningService
from caucus_race.usecase.project import BacklogService, ProjectService
from caucus_race.usecase.review import RetroService, ReviewService
from caucus_race.usecase.sprint import SprintService

app = typer.Typer(no_args_is_help=True, help="AI エージェントによるスプリント開発のオーケストレーター")
project_app = typer.Typer(no_args_is_help=True, help="プロジェクト")
story_app = typer.Typer(no_args_is_help=True, help="ストーリー（バックログ）")
sprint_app = typer.Typer(no_args_is_help=True, help="スプリント")
task_app = typer.Typer(no_args_is_help=True, help="タスク")
spike_app = typer.Typer(no_args_is_help=True, help="スパイク")
review_app = typer.Typer(no_args_is_help=True, help="スプリントレビュー")
feedback_app = typer.Typer(no_args_is_help=True, help="フィードバック")
retro_app = typer.Typer(no_args_is_help=True, help="レトロスペクティブ")
plan_app = typer.Typer(no_args_is_help=True, help="AI プランナー")
for sub, name in [
    (project_app, "project"),
    (story_app, "story"),
    (sprint_app, "sprint"),
    (task_app, "task"),
    (spike_app, "spike"),
    (review_app, "review"),
    (feedback_app, "feedback"),
    (retro_app, "retro"),
    (plan_app, "plan"),
]:
    app.add_typer(sub, name=name)


@cache
def container() -> Injector:
    return build_injector()


def get[T](cls: type[T]) -> T:
    return container().get(cls)


def repo() -> Repository:
    return get(Repository)  # type: ignore[type-abstract]


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
    )
    try:
        app()
    except (DomainError, WorkspaceError, PlannerError) as e:
        typer.secho(f"エラー: {e}", fg=typer.colors.RED, err=True)
        raise SystemExit(1) from e


def _story_line(story: Story) -> str:
    return f"{story.id:<7} [{story.status:<9}] {story.title}"


# --- project ---


ExecutorOpt = Annotated[str | None, typer.Option("--executor", help="実行器の種類（fake / vuoi）")]
WorkflowOpt = Annotated[str | None, typer.Option("--workflow", help="vuoi のワークフロー名（既定 dev）")]
TestCommandOpt = Annotated[
    list[str] | None, typer.Option("--test-command", help="vuoi に渡すテストコマンド（複数可）")
]

PlannerOpt = Annotated[str | None, typer.Option("--planner", help="プランナーの種類（manual / claude）")]
ModelOpt = Annotated[str | None, typer.Option("--model", help="プランナーが使う Claude のモデル")]


def _planner_config(planner: str | None, model: str | None) -> dict[str, Any]:
    config: dict[str, Any] = {}
    if planner is not None:
        config["type"] = planner
    if model is not None:
        config["model"] = model
    return config


def _executor_config(
    executor: str | None, workflow: str | None, test_commands: list[str] | None
) -> dict[str, Any]:
    config: dict[str, Any] = {}
    if executor is not None:
        config["type"] = executor
    if workflow is not None:
        config["workflow"] = workflow
    if test_commands:
        config["test_commands"] = test_commands
    return config


@project_app.command("add")
def project_add(
    name: str,
    repo_path: Annotated[Path, typer.Option("--repo", help="対象リポジトリのパス")],
    description: Annotated[str, typer.Option("--description", "-d")] = "",
    base_branch: Annotated[str, typer.Option("--base")] = "main",
    max_stories: Annotated[int, typer.Option("--max-stories")] = 5,
    executor: ExecutorOpt = "fake",
    workflow: WorkflowOpt = None,
    test_commands: TestCommandOpt = None,
    planner: PlannerOpt = "manual",
    model: ModelOpt = None,
) -> None:
    """プロジェクトを登録する。"""
    project = get(ProjectService).register(
        name,
        repo_path.expanduser(),
        description=description,
        base_branch=base_branch,
        max_stories_per_sprint=max_stories,
        executor_config=_executor_config(executor, workflow, test_commands),
        planner_config=_planner_config(planner, model),
    )
    typer.echo(f"{project.id} {project.name} を登録しました")


@project_app.command("set")
def project_set(
    name: str,
    description: Annotated[str | None, typer.Option("--description", "-d")] = None,
    base_branch: Annotated[str | None, typer.Option("--base")] = None,
    max_stories: Annotated[int | None, typer.Option("--max-stories")] = None,
    executor: ExecutorOpt = None,
    workflow: WorkflowOpt = None,
    test_commands: TestCommandOpt = None,
    planner: PlannerOpt = None,
    model: ModelOpt = None,
) -> None:
    """プロジェクトの設定を変更する。指定した項目だけを更新する。"""
    project = get(ProjectService).configure(
        name,
        description=description,
        base_branch=base_branch,
        max_stories_per_sprint=max_stories,
        executor_config=_executor_config(executor, workflow, test_commands),
        planner_config=_planner_config(planner, model),
    )
    typer.echo(
        f"{project.name} を更新しました: base={project.base_branch} "
        f"executor={project.executor_config} planner={project.planner_config}"
    )


@project_app.command("list")
def project_list() -> None:
    """プロジェクトを一覧する。"""
    for p in get(ProjectService).list_projects():
        sprint = latest_sprint(repo(), p)
        state = f"sprint #{sprint.number} {sprint.status}" if sprint else "スプリントなし"
        typer.echo(f"{p.id:<7} {p.name:<20} {state:<24} {p.repo_path}")


# --- story ---


@story_app.command("add")
def story_add(
    project: str,
    title: str,
    description: Annotated[str, typer.Option("--description", "-d")] = "",
    criteria: Annotated[
        list[str] | None, typer.Option("--criteria", "-a", help="受け入れ条件（複数可）")
    ] = None,
) -> None:
    """バックログにストーリーを追加する。"""
    story = get(BacklogService).add_story(
        project, title, description=description, acceptance_criteria=criteria or []
    )
    typer.echo(f"{story.id} を追加しました")


@story_app.command("list")
def story_list(project: str) -> None:
    """バックログを一覧する。"""
    for story in get(BacklogService).backlog(project):
        typer.echo(_story_line(story))


@story_app.command("show")
def story_show(story_id: str) -> None:
    """ストーリーの詳細を表示する。"""
    r = repo()
    story = r.get(Story, story_id)
    typer.echo(_story_line(story))
    typer.echo(f"起票元: {story.source}" + (f"（{story.source_ref}）" if story.source_ref else ""))
    if story.description:
        typer.echo(f"\n{story.description}")
    sections = [
        ("受け入れ条件", story.acceptance_criteria),
        ("決定事項", story.decisions),
        ("タスク", [f"{t.id} [{t.status}] {t.title}" for t in r.query(Task, story_id=story.id)]),
        ("スパイク", [f"{s.id} [{s.status}] {s.question}" for s in r.query(Spike, story_id=story.id)]),
        ("コミット", story.commits),
    ]
    for title, items in sections:
        if items:
            typer.echo(f"\n{title}:")
            for item in items:
                typer.echo(f"  - {item}")
    if story.wip_branch:
        typer.echo(f"\n途中の変更: {story.wip_branch}")
    if story.result:
        typer.echo(f"\n実行結果: {story.result.status} {story.result.summary}")
        for label, value in [("中断理由", story.result.blocked_reason), ("エラー", story.result.error)]:
            if value:
                typer.echo(f"{label}: {value}")


# --- sprint ---


@sprint_app.command("new")
def sprint_new(project: str, goal: Annotated[str, typer.Option("--goal", "-g")]) -> None:
    """スプリントを作成する（planning）。"""
    sprint = get(PlanningService).new_sprint(project, goal)
    typer.echo(f"スプリント #{sprint.number} を作成しました")


@sprint_app.command("add")
def sprint_add(project: str, story_ids: list[str]) -> None:
    """バックログのストーリーをスプリントに追加する。"""
    for story_id in story_ids:
        get(PlanningService).add_story(project, story_id)
        typer.echo(f"{story_id} を追加しました")


@sprint_app.command("remove")
def sprint_remove(project: str, story_ids: list[str]) -> None:
    """ストーリーをスプリントから外し、バックログに戻す。"""
    for story_id in story_ids:
        get(PlanningService).remove_story(project, story_id)
        typer.echo(f"{story_id} をバックログに戻しました")


@sprint_app.command("show")
def sprint_show(project: str) -> None:
    """進行中のスプリントを表示する。"""
    r = repo()
    sprint = require_open_sprint(r, project_by_name(r, project))
    stories = sprint_stories(r, sprint)
    typer.echo(f"スプリント #{sprint.number} [{sprint.status}] {sprint.goal}")
    if sprint.branch:
        typer.echo(f"ブランチ: {sprint.branch}")
    if sprint.plan_comment:
        typer.echo(f"差し戻しコメント: {sprint.plan_comment}")
    for story in stories:
        typer.echo(_story_line(story))
    spikes = open_spikes(r, stories)
    if spikes:
        typer.echo(f"\n未解決のスパイク: {len(spikes)} 件（caucus spike list {project}）")


@sprint_app.command("approve")
def sprint_approve(
    project: str,
    start: Annotated[bool, typer.Option("--start", help="承認後、バックグラウンドで開始する")] = False,
) -> None:
    """スプリント計画を承認する。"""
    sprint = get(PlanningService).approve(project)
    typer.echo(f"スプリント #{sprint.number} を承認しました")
    if start:
        sprint_start(project, detach=True)


@sprint_app.command("reject")
def sprint_reject(project: str, comment: Annotated[str, typer.Option("--comment", "-m")]) -> None:
    """スプリント計画を差し戻す。"""
    sprint = get(PlanningService).reject(project, comment)
    typer.echo(f"スプリント #{sprint.number} を差し戻しました")


DetachOpt = Annotated[
    bool, typer.Option("--detach", "-D", help="バックグラウンドで実行する（ログは CAUCUS_HOME/logs）")
]


def _lock(project: str) -> RunLock:
    return RunLock(get(Settings).run_dir, project)


def _run_foreground(project: str, log_path: Path | None = None) -> None:
    """ロックを取ってスプリントを最後まで実行する。

    SIGTERM で止められる。実行中だったストーリーは resume で再実行される。
    """

    def _terminate(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _terminate)
    with _lock(project).hold(log_path):
        r = repo()
        sprint = require_open_sprint(r, project_by_name(r, project))
        if sprint.status != SprintStatus.ACTIVE:
            raise DomainError(f"スプリント #{sprint.number} は実行中ではありません（{sprint.status}）")
        try:
            sprint = get(Orchestrator).run(sprint.id)
        except Exception as e:
            get(Notifier).notify(  # type: ignore[type-abstract]
                HumanActionRequired(
                    kind="runner_error",
                    project=project,
                    message=f"スプリント #{sprint.number} の実行が止まりました: {e}",
                )
            )
            raise
    typer.echo(f"スプリント #{sprint.number} は {sprint.status} になりました")


def _run(project: str, detach: bool) -> None:
    if not detach:
        _run_foreground(project)
        return
    lock = _lock(project)
    if (info := lock.running()) is not None:
        raise DomainError(f"このプロジェクトのスプリントは既に実行中です（pid {info.pid}）")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = get(Settings).log_dir / f"{project}-{stamp}.log"
    pid = spawn_detached(runner_argv(project, log_path), log_path)
    typer.echo(f"バックグラウンドで実行しています（pid {pid}）")
    typer.echo(f"ログ: {log_path}")


@sprint_app.command("start")
def sprint_start(project: str, detach: DetachOpt = False) -> None:
    """スプリントを開始し、ストーリーを順に実行する。"""
    if (info := _lock(project).running()) is not None:
        raise DomainError(f"このプロジェクトのスプリントは既に実行中です（pid {info.pid}）")
    sprint = get(SprintService).start(project)
    typer.echo(f"スプリント #{sprint.number} を開始しました（{sprint.branch}）")
    _run(project, detach)


@sprint_app.command("resume")
def sprint_resume(project: str, detach: DetachOpt = False) -> None:
    """停止したスプリントの実行を再開する。"""
    _run(project, detach)


@sprint_app.command("run", hidden=True)
def sprint_run(
    project: str, log: Annotated[Path | None, typer.Option("--log", help="ログの保存先（表示用）")] = None
) -> None:
    """（内部用）スプリントを実行する。--detach で起動されたプロセスが呼ぶ。"""
    _run_foreground(project, log)


@sprint_app.command("stop")
def sprint_stop(project: str) -> None:
    """バックグラウンドで実行中のスプリントを止める。実行中だったストーリーは resume で再実行される。"""
    info = _lock(project).running()
    if info is None:
        raise DomainError(f"{project} のスプリントは実行されていません")
    stop_run(info)
    typer.echo(f"停止を指示しました（pid {info.pid}）。再開するには caucus sprint resume {project}")


@sprint_app.command("logs")
def sprint_logs(project: str, lines: Annotated[int, typer.Option("--lines", "-n")] = 40) -> None:
    """実行中（または直近）のスプリントのログの末尾を表示する。"""
    info = _lock(project).running()
    log_path = info.log_path if info and info.log_path else None
    if log_path is None:
        candidates = sorted(get(Settings).log_dir.glob(f"{project}-*.log"))
        log_path = candidates[-1] if candidates else None
    if log_path is None or not log_path.exists():
        raise DomainError(f"{project} のログがありません")
    typer.echo(f"==> {log_path}")
    for line in log_path.read_text(encoding="utf-8").splitlines()[-lines:]:
        typer.echo(line)


# --- task ---


@task_app.command("add")
def task_add(
    story_id: str, title: str, description: Annotated[str, typer.Option("--description", "-d")] = ""
) -> None:
    """ストーリーにタスクを追加する。"""
    task = get(PlanningService).add_task(story_id, title, description)
    typer.echo(f"{task.id} を追加しました")


# --- spike ---


@spike_app.command("add")
def spike_add(
    story_id: str,
    question: str,
    context: Annotated[str, typer.Option("--context", "-c")] = "",
    options: Annotated[list[str] | None, typer.Option("--option", "-o")] = None,
    recommendation: Annotated[str | None, typer.Option("--recommend", "-r")] = None,
) -> None:
    """ストーリーにスパイクを起票する。"""
    spike = get(PlanningService).add_spike(
        story_id, question, context=context, options=options or [], recommendation=recommendation
    )
    typer.echo(f"{spike.id} を起票しました")


@spike_app.command("list")
def spike_list(project: str) -> None:
    """プロジェクトの未解決のスパイクを一覧する。"""
    r = repo()
    p = project_by_name(r, project)
    stories = r.query(Story, project_id=p.id)
    spikes = r.query(Spike, story_id=[s.id for s in stories], status=SpikeStatus.OPEN) if stories else []
    for spike in spikes:
        typer.echo(f"{spike.id:<6} ({spike.story_id}, {spike.raised_by}) {spike.question}")
        if spike.context:
            typer.echo(f"       背景: {spike.context}")
        for option in spike.options:
            typer.echo(f"       - {option}")
        if spike.recommendation:
            typer.echo(f"       推奨: {spike.recommendation}")


@spike_app.command("answer")
def spike_answer(
    spike_id: str,
    answer: str,
    refine: Annotated[
        bool, typer.Option("--refine/--no-refine", help="回答後に AI にタスクを見直させる")
    ] = True,
) -> None:
    """スパイクに回答する。回答はストーリーの決定事項に追加される。

    AI プランナーが設定されていて、ストーリーの未解決のスパイクがなくなった場合は、AI がタスクを見直す。
    """
    spike = get(PlanningService).answer_spike(spike_id, answer)
    typer.echo(f"{spike_id} に回答しました")
    r = repo()
    story = r.get(Story, spike.story_id)
    project = r.get(Project, story.project_id)
    remaining = r.query(Spike, story_id=story.id, status=SpikeStatus.OPEN)
    if refine and not remaining and project.planner_config.get("type", "manual") != "manual":
        typer.echo(f"AI が {story.id} を見直しています...")
        get(AIPlanningService).refine(story.id)
        typer.echo(f"{story.id} を見直しました（caucus story show {story.id}）")


# --- plan（AI） ---


@plan_app.command("sprint")
def plan_sprint(project: str) -> None:
    """AI にスプリント計画を作らせる。差し戻された計画があれば、コメントに従って作り直させる。"""
    typer.echo("AI が計画を作成しています...")
    sprint = get(AIPlanningService).plan(project)
    typer.echo(f"スプリント #{sprint.number} の計画を作成しました")
    sprint_show(project)


@plan_app.command("refine")
def plan_refine(story_id: str) -> None:
    """スパイクの回答を踏まえて、AI にストーリーのタスクと受け入れ条件を見直させる。"""
    get(AIPlanningService).refine(story_id)
    story_show(story_id)


@plan_app.command("feedback")
def plan_feedback(project: str) -> None:
    """レビュー中のフィードバックを AI にストーリーへ変換させる。"""
    for story in get(AIPlanningService).convert_pending_feedback(project):
        typer.echo(_story_line(story))


@spike_app.command("drop")
def spike_drop(spike_id: str, reason: Annotated[str, typer.Option("--reason")] = "") -> None:
    """スパイクを取り下げる。"""
    get(PlanningService).drop_spike(spike_id, reason)
    typer.echo(f"{spike_id} を取り下げました")


# --- review / feedback ---


@review_app.command("show")
def review_show(
    project: str,
    regenerate: Annotated[bool, typer.Option("--regenerate", help="確認手順を AI に書き直させる")] = False,
) -> None:
    """レビュー資料を生成して表示する。AI プランナーが設定されていれば確認手順も載せる。"""
    path, text = get(ReviewService).report(project, regenerate_guide=regenerate)
    typer.echo(text)
    typer.echo(f"（保存先: {path}）")


@review_app.command("done")
def review_done(project: str) -> None:
    """レビューを完了し、スプリントを終了する。"""
    sprint = get(ReviewService).close(project)
    typer.echo(f"スプリント #{sprint.number} を終了しました")


@feedback_app.command("add")
def feedback_add(
    project: str, body: str, story_id: Annotated[str | None, typer.Option("--story")] = None
) -> None:
    """レビューのフィードバックを登録する。"""
    feedback = get(ReviewService).add_feedback(project, body, story_id)
    typer.echo(f"{feedback.id} を登録しました")


@feedback_app.command("list")
def feedback_list(project: str) -> None:
    """直近のスプリントのフィードバックを一覧する。"""
    r = repo()
    sprint = latest_sprint(r, project_by_name(r, project))
    for fb in r.query(Feedback, sprint_id=sprint.id) if sprint else []:
        converted = f" → {fb.converted_story_id}" if fb.converted_story_id else ""
        typer.echo(f"{fb.id:<6} {fb.body.splitlines()[0]}{converted}")


@feedback_app.command("convert")
def feedback_convert(
    feedback_id: str, title: Annotated[str | None, typer.Option("--title", "-t")] = None
) -> None:
    """フィードバックをバックログのストーリーに変換する。"""
    story = get(ReviewService).convert_feedback(feedback_id, title)
    typer.echo(f"{story.id} を追加しました")


# --- retro ---


@retro_app.command("add")
def retro_add(
    project: str, body: str, author: Annotated[Author, typer.Option("--author")] = Author.HUMAN
) -> None:
    """レトロ記録を追加する。"""
    note = get(RetroService).add(project, body, author)
    typer.echo(f"{note.id} を追加しました")


@retro_app.command("list")
def retro_list(project: str) -> None:
    """レトロ記録を一覧する。"""
    for note in get(RetroService).notes(project):
        typer.echo(f"{note.id:<6} ({note.author}) {note.body}")


# --- status ---


@app.command("status")
def status() -> None:
    """全プロジェクトの状態と、人間の対応待ちを表示する。"""
    r = repo()
    for p in r.query(Project):
        sprint = latest_sprint(r, p)
        if sprint is None:
            typer.echo(f"{p.name}: スプリントなし")
            continue
        stories = sprint_stories(r, sprint)
        todo: list[str] = []
        spikes = open_spikes(r, stories)
        if spikes:
            todo.append(f"スパイク回答 {len(spikes)} 件")
        if sprint.status == SprintStatus.PLANNING:
            todo.append("計画の承認")
        if sprint.status == SprintStatus.REVIEW:
            todo.append("スプリントレビュー")
        running = _lock(p.name).running()
        if sprint.status == SprintStatus.ACTIVE:
            state = f"実行中（pid {running.pid}）" if running else "停止中（caucus sprint resume で再開）"
            todo_or_state = f"  {state}"
        else:
            todo_or_state = ""
        suffix = f"  要対応: {', '.join(todo)}" if todo else ""
        typer.echo(
            f"{p.name}: スプリント #{sprint.number} [{sprint.status}] {sprint.goal}{todo_or_state}{suffix}"
        )


@app.command("notifications")
def notifications(limit: Annotated[int, typer.Option("--limit", "-n")] = 20) -> None:
    """最近の通知を表示する。"""
    history = HistoryNotifier(get(Settings).data_dir / "notifications.jsonl")
    for record in history.read(limit):
        typer.echo(f"{record['at'][:19]} [{record['kind']}] {record['project']}: {record['message']}")


if __name__ == "__main__":
    main()
