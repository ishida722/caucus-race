"""スプリント実行プロセスの管理。

プロジェクトごとにロックファイルを持ち、同じプロジェクトのスプリントを二重に実行しないようにする。
別のプロジェクトは別プロセスで並行に実行できる。
"""

import fcntl
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from caucus_race.domain.errors import DomainError


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
