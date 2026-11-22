"""AI プランナーによるスプリント計画を、別プロセスで実行するジョブ。

AI プランナーは数分かかることがあるため、MCP のツール呼び出しの中で待つとタイムアウトする。
ツールはジョブを起動してすぐに返し、エージェントは状態を後から問い合わせる。

- ジョブはプロジェクトごとに 1 つまで。ロックファイルの flock で「実行中」を表す。
  起動側がロックを取り、ロックを持ったファイル記述子を子プロセスに引き継ぐ。
  子プロセスが終われば（異常終了でも）ロックは外れる。
- 状態（実行中・成功・失敗）は JSON ファイルに書く。子プロセスが終了時に結果を書き込む。
  ロックが外れているのに「実行中」のままなら、途中で異常終了したとみなす。
- 別プロセスなので、MCP サーバー（エージェントのセッション）が終わっても計画の作成は続く。
- ロック・状態・ログのファイル名には、プロジェクト名を URL エンコードして使う。プロジェクト名は
  検証されていないため、`/` や `..` を含んでも run_dir / log_dir の外にファイルを作らない。
"""

import fcntl
import hashlib
import logging
import os
import subprocess
import sys
import threading
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field, ValidationError

from caucus_race.domain.entities import Sprint, now
from caucus_race.domain.errors import DomainError
from caucus_race.port.planner import PlannerError
from caucus_race.port.workspace import WorkspaceError
from caucus_race.usecase.ai_planning import AIPlanningService
from caucus_race.usecase.common import sprint_stories

logger = logging.getLogger(__name__)


class PlanJobState(BaseModel):
    project: str
    status: Literal["running", "succeeded", "failed"]
    started_at: datetime
    finished_at: datetime | None = None
    # 成功したときの計画
    sprint_number: int | None = None
    goal: str | None = None
    story_ids: list[str] = Field(default_factory=list)
    # 失敗したときの理由
    error: str | None = None
    log_path: Path | None = None


class Launcher(Protocol):
    def __call__(self, argv: list[str], log_path: Path, lock_fd: int) -> None:
        """argv を別プロセスで起動する。lock_fd はロックを持ったファイル記述子で、子プロセスに引き継ぐ。"""
        ...


def launch_detached(argv: list[str], log_path: Path, lock_fd: int) -> None:
    """端末から切り離したプロセスとして起動する。出力は log_path に書く。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            pass_fds=(lock_fd,),
            env=os.environ.copy(),
        )
    # 終了したプロセスを回収する（ゾンビを残さない）
    threading.Thread(target=proc.wait, daemon=True).start()


# ファイル名の長さの上限（多くのファイルシステムで 255 バイト）から、接尾辞
# （".plan.json.tmp"、"-plan-YYYYmmdd-HHMMSS.log"）の分を引いた、名前の部分の上限
MAX_STEM_BYTES = 200


def _escape_uppercase(quoted: str) -> str:
    """URL エンコード済みの文字列の英大文字を %XX にする。

    エンコードの結果は「英小文字・数字・記号」と「%XX（XX は大文字の 16 進数）」だけになる。
    大文字小文字を同一視しても % で区切りが分かるため、元の名前ごとに異なるファイル名のままになる。
    """
    out: list[str] = []
    i = 0
    while i < len(quoted):
        if quoted[i] == "%":  # 既存のエスケープはそのまま残す
            out.append(quoted[i : i + 3])
            i += 3
            continue
        c = quoted[i]
        out.append(f"%{ord(c):02X}" if "A" <= c <= "Z" else c)
        i += 1
    return "".join(out)


def file_stem(project_name: str) -> str:
    """プロジェクト名を、1 つのファイル名として安全に使える形にする。

    - `/` や `..` を含んでも、1 つのファイル名になる（URL エンコードする）
    - 大文字小文字だけが違う名前も、大文字小文字を区別しないファイルシステム（macOS の既定）で
      別のファイル名になる（英大文字もエンコードする）
    - 長い名前（非 ASCII はエンコードで 9 倍になる）でも、ファイル名の長さの上限を超えない。
      上限を超える場合は先頭を残し、名前全体のハッシュを付ける。"%%" はエンコードの結果には現れないため、
      短い名前の結果と重ならない
    """
    stem = _escape_uppercase(urllib.parse.quote(project_name, safe="", errors="surrogatepass"))
    if stem.strip(".") == "":
        stem = stem.replace(".", "%2E")
    if len(stem) > MAX_STEM_BYTES:
        digest = hashlib.sha256(project_name.encode("utf-8", "surrogatepass")).hexdigest()
        stem = f"{stem[: MAX_STEM_BYTES - 66]}%%{digest}"
    return stem


def plan_runner_argv(project_name: str) -> list[str]:
    # "--" 以降は位置引数として扱われる（"-x" のような名前をオプションと解釈させない）
    return [sys.executable, "-m", "caucus_race.cli.main", "plan", "run", "--", project_name]


class PlanJobs:
    def __init__(self, run_dir: Path, log_dir: Path, launcher: Launcher = launch_detached) -> None:
        self.run_dir = run_dir
        self.log_dir = log_dir
        self.launcher = launcher

    def _lock_path(self, project_name: str) -> Path:
        return self.run_dir / f"{file_stem(project_name)}.plan.lock"

    def _state_path(self, project_name: str) -> Path:
        return self.run_dir / f"{file_stem(project_name)}.plan.json"

    def _write(self, state: PlanJobState) -> None:
        path = self._state_path(state.project)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)

    def _read(self, project_name: str) -> PlanJobState | None:
        path = self._state_path(project_name)
        if not path.exists():
            return None
        return PlanJobState.model_validate_json(path.read_text(encoding="utf-8"))

    def running(self, project_name: str) -> bool:
        path = self._lock_path(project_name)
        if not path.exists():
            return False
        fd = os.open(path, os.O_RDONLY)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def start(self, project_name: str) -> PlanJobState:
        """計画の作成を別プロセスで始める。同じプロジェクトで実行中なら DomainError。"""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path(project_name), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DomainError(f"{project_name} のスプリント計画は既に作成中です") from None
            started = now()
            log_path = self.log_dir / f"{file_stem(project_name)}-plan-{started:%Y%m%d-%H%M%S}.log"
            state = PlanJobState(
                project=project_name, status="running", started_at=started, log_path=log_path
            )
            self._write(state)
            try:
                self.launcher(plan_runner_argv(project_name), log_path, fd)
            except Exception as e:
                # プロセスは起動していない。理由を状態に残し、running のままにしない
                reason = str(e) if isinstance(e, OSError) else f"{type(e).__name__}: {e}"
                self._write(
                    state.model_copy(update={"status": "failed", "finished_at": now(), "error": reason})
                )
                if isinstance(e, OSError):
                    raise DomainError(f"計画の作成を開始できませんでした: {e}") from e
                raise
            return state
        finally:
            # 子プロセスが同じロックを持ち続ける
            os.close(fd)

    def status(self, project_name: str) -> PlanJobState | None:
        """直近のジョブの状態。一度も実行していなければ None。"""
        state = self._read(project_name)
        if state is None or state.status != "running" or self.running(project_name):
            return state
        # 読んでからロックを確かめるまでの間に、ジョブが結果を書いて終わった可能性がある。読み直す
        state = self._read(project_name)
        if state is None or state.status != "running":
            return state
        log = f"（ログ: {state.log_path}）" if state.log_path else ""
        return state.model_copy(
            update={"status": "failed", "error": f"計画の作成が途中で異常終了しました{log}"}
        )

    def finish(
        self, project_name: str, *, sprint: Sprint | None, story_ids: list[str], error: str | None
    ) -> None:
        try:
            previous = self._read(project_name)
        except ValidationError, ValueError, OSError:
            # 状態ファイルが読めなくても、計画の結果は書く（DB と状態を食い違わせない）
            logger.warning("計画ジョブの状態ファイルが読めないため、作り直します", exc_info=True)
            previous = None
        state = previous or PlanJobState(project=project_name, status="running", started_at=now())
        if error is None and sprint is not None:
            update = {
                "status": "succeeded",
                "sprint_number": sprint.number,
                "goal": sprint.goal,
                "story_ids": story_ids,
            }
        else:
            update = {"status": "failed", "error": error or "計画を作成できませんでした"}
        self._write(state.model_copy(update={**update, "finished_at": now()}))


def run_plan_job(service: AIPlanningService, jobs: PlanJobs, project_name: str) -> bool:
    """（子プロセスで）計画を作り、結果をジョブの状態に書く。成功したら True。"""
    try:
        sprint = service.plan(project_name)
    # 利用者に見せるエラーは CLI（cli/main.py）と同じ型にそろえる。WorkspaceError は今の計画の処理からは
    # 起きないが、プランナーが将来 Workspace を使っても同じ扱いにするために含める
    except (DomainError, PlannerError, WorkspaceError) as e:
        logger.error("スプリント計画を作成できませんでした: %s", e)
        jobs.finish(project_name, sprint=None, story_ids=[], error=str(e))
        return False
    except Exception as e:
        logger.exception("スプリント計画の作成中に想定外のエラーが発生しました")
        jobs.finish(project_name, sprint=None, story_ids=[], error=f"想定外のエラー: {e}")
        return False
    jobs.finish(
        project_name,
        sprint=sprint,
        story_ids=[s.id for s in sprint_stories(service.repo, sprint)],
        error=None,
    )
    return True
