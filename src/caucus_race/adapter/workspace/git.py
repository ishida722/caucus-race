import subprocess
from pathlib import Path

from caucus_race.port.workspace import WorkspaceError


class GitWorkspace:
    """`git` コマンドを呼んで対象リポジトリを操作する。"""

    def _git(self, repo: Path, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise WorkspaceError(f"git {' '.join(args)} に失敗しました: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def is_clean(self, repo: Path) -> bool:
        return self._git(repo, "status", "--porcelain") == ""

    def head(self, repo: Path) -> str:
        return self._git(repo, "rev-parse", "HEAD")

    def current_branch(self, repo: Path) -> str | None:
        name = self._git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        return None if name == "HEAD" else name

    def branch_exists(self, repo: Path, name: str) -> bool:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
            capture_output=True,
            check=False,
        )
        return proc.returncode == 0

    def create_branch(self, repo: Path, name: str, base: str) -> None:
        self._git(repo, "switch", "-c", name, base)

    def checkout(self, repo: Path, name: str) -> None:
        self._git(repo, "switch", name)

    def commit_all(self, repo: Path, message: str) -> str | None:
        if self.is_clean(repo):
            return None
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-m", message)
        return self.head(repo)

    def save_to_branch(self, repo: Path, name: str) -> None:
        self._git(repo, "branch", "-f", name, "HEAD")

    def reset_hard(self, repo: Path, sha: str) -> None:
        self._git(repo, "reset", "--hard", sha)
        self._git(repo, "clean", "-fd")

    def diff_stat(self, repo: Path, base: str, head: str) -> str:
        return self._git(repo, "diff", "--stat", base, head)

    def commits_between(self, repo: Path, base: str, head: str) -> list[str]:
        out = self._git(repo, "rev-list", "--reverse", f"{base}..{head}")
        return out.splitlines() if out else []
