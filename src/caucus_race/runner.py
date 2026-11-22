"""スプリント実行プロセスの管理。

プロジェクトごとにロックファイルを持ち、同じプロジェクトのスプリントを二重に実行しないようにする。
別のプロジェクトは別プロセスで並行に実行できる。
"""

import fcntl
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO

from caucus_race.domain.errors import DomainError
from caucus_race.plan_job import file_stem


@dataclass(frozen=True)
class RunInfo:
    pid: int
    log_path: Path | None


class RunLock:
    def __init__(self, run_dir: Path, project_name: str) -> None:
        self.path = run_dir / f"{project_name}.lock"

    @contextmanager
    def hold(self, log_path: Path | None = None) -> Iterator[None]:
        """ロックを取って保持する。他のプロセスが保持していれば DomainError。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f: IO[str] = self.path.open("a+", encoding="utf-8")
        try:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                info = self.running()
                pid = f"（pid {info.pid}）" if info else ""
                raise DomainError(f"このプロジェクトのスプリントは既に実行中です{pid}") from None
            f.seek(0)
            f.truncate()
            f.write(f"{os.getpid()}\n{log_path or ''}\n")
            f.flush()
            try:
                yield
            finally:
                f.seek(0)
                f.truncate()
                fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()

    def running(self) -> RunInfo | None:
        """実行中ならそのプロセスの情報を返す。"""
        if not self.path.exists():
            return None
        with self.path.open("r", encoding="utf-8") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                lines = f.read().splitlines()
                if not lines or not lines[0].isdigit():
                    return RunInfo(pid=0, log_path=None)
                log = Path(lines[1]) if len(lines) > 1 and lines[1] else None
                return RunInfo(pid=int(lines[0]), log_path=log)
            fcntl.flock(f, fcntl.LOCK_UN)
            return None


def spawn_detached(argv: list[str], log_path: Path) -> int:
    """端末から切り離したプロセスとして起動する。出力は log_path に書く。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=os.environ.copy(),
        )
    # 終了したプロセスを回収する（MCP サーバーのように長く動くプロセスから起動しても、ゾンビを残さない）
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc.pid


def runner_argv(project_name: str, log_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "caucus_race.cli.main",
        "sprint",
        "run",
        project_name,
        "--log",
        str(log_path),
    ]


def stop(info: RunInfo) -> None:
    """実行プロセスとその子プロセス（vuoi・claude）をまとめて止める。"""
    if info.pid <= 0:
        raise DomainError("実行中のプロセスを特定できません")
    try:
        os.killpg(info.pid, signal.SIGTERM)  # バックグラウンド実行はプロセスグループの先頭
    except ProcessLookupError:
        try:
            os.kill(info.pid, signal.SIGTERM)  # 端末から直接実行している場合
        except ProcessLookupError:
            pass


def refine_argv(story_id: str) -> list[str]:
    # "--" 以降は位置引数として扱われる
    return [sys.executable, "-m", "caucus_race.cli.main", "plan", "refine", "--", story_id]


@dataclass(frozen=True)
class Launched:
    """バックグラウンドで起動したプロセス。表示は呼び出し側（CLI・MCP）で行う。"""

    pid: int
    log_path: Path


Spawn = Callable[[list[str], Path], int]


class BackgroundRunner:
    """スプリントの実行と、スパイク回答後の見直しをバックグラウンドで起動・停止する。CLI と MCP で共有する。

    spawn はプロセスの起動（プロセス境界）。テストでは差し替える。
    """

    def __init__(
        self,
        run_dir: Path,
        log_dir: Path,
        spawn: Spawn = spawn_detached,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.run_dir = run_dir
        self.log_dir = log_dir
        self.spawn = spawn
        self.clock = clock  # ログのファイル名に入れる時刻（プロセス境界。テストで差し替える）

    def lock(self, project_name: str) -> RunLock:
        return RunLock(self.run_dir, project_name)

    def running(self, project_name: str) -> RunInfo | None:
        return self.lock(project_name).running()

    def _launch_marker(self, project_name: str) -> Path:
        return self.run_dir / f"{file_stem(project_name)}.launched"

    def _launching(self, project_name: str) -> int | None:
        """起動した実行プロセスが、まだ実行ロックを取らずに生きていれば、その pid を返す。

        実行ロックは起動された子プロセスが取るため、起動してからロックを取るまでの間は、ロックだけを見ると
        「実行中でない」に見える。その間に開始・再開をもう一度受け付けると二重に起動してしまうため、
        起動した pid を印のファイルに残し、そのプロセスが生きている間は実行中とみなす。
        プロセスが終われば（ロックを取れずに終わった場合も）印は無視される。

        起動の後に実行ロックが取られた（ロックファイルが印より後に更新された）なら、起動したプロセスはもうロックを
        取ったか、取って終わっている。その後に印の pid が別のプロセスに再利用されても実行中とみなさないよう、
        その印は無効として消す（RunLock はロックを取るときと手放すときにロックファイルを書き換える）。
        """
        marker = self._launch_marker(project_name)
        try:
            pid = int(marker.read_text(encoding="utf-8").strip())
            marked_at = marker.stat().st_mtime_ns
        except OSError, ValueError:
            return None
        lock_path = self.lock(project_name).path
        try:
            locked_after_launch = lock_path.stat().st_mtime_ns > marked_at
        except OSError:
            locked_after_launch = False
        if locked_after_launch or pid <= 0:
            marker.unlink(missing_ok=True)
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            return pid  # 存在はしている
        return pid

    def ensure_not_running(self, project_name: str) -> None:
        if (info := self.running(project_name)) is not None:
            raise DomainError(f"このプロジェクトのスプリントは既に実行中です（pid {info.pid}）")
        if (pid := self._launching(project_name)) is not None:
            raise DomainError(f"このプロジェクトのスプリントは既に実行中です（pid {pid}）")

    def start_run(self, project_name: str) -> Launched:
        """スプリントの実行プロセスを起動する。

        既に実行中（起動直後でロックを取る前を含む）なら DomainError。
        """
        self.ensure_not_running(project_name)
        stamp = self.clock().strftime("%Y%m%d-%H%M%S")
        log_path = self.log_dir / f"{project_name}-{stamp}.log"
        pid = self.spawn(runner_argv(project_name, log_path), log_path)
        marker = self._launch_marker(project_name)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{pid}\n", encoding="utf-8")
        return Launched(pid=pid, log_path=log_path)

    def stop_run(self, project_name: str) -> RunInfo:
        """実行中のスプリントのプロセスに停止を指示する。実行中でなければ DomainError。

        起動した直後でまだ実行ロックを取っていないプロセスも止める（起動したプロセスはプロセスグループの先頭）。
        """
        info = self.running(project_name)
        if info is None and (pid := self._launching(project_name)) is not None:
            info = RunInfo(pid=pid, log_path=None)
        if info is None:
            raise DomainError(f"{project_name} のスプリントは実行されていません")
        stop(info)
        return info

    def start_refine(self, project_name: str, story_id: str) -> Launched:
        """ストーリーの見直し（caucus plan refine）を起動する。

        ログは log_dir/refine/ に置く（`caucus sprint logs` が拾う、log_dir 直下の
        スプリントのログと混ぜない）。
        """
        stamp = self.clock().strftime("%Y%m%d-%H%M%S")
        log_path = self.log_dir / "refine" / f"{file_stem(project_name)}-{file_stem(story_id)}-{stamp}.log"
        pid = self.spawn(refine_argv(story_id), log_path)
        return Launched(pid=pid, log_path=log_path)
