from pathlib import Path
from typing import Protocol


class WorkspaceError(Exception):
    pass


class Workspace(Protocol):
    """対象リポジトリの git 操作。"""

    def is_clean(self, repo: Path) -> bool: ...

    def head(self, repo: Path) -> str: ...

    def current_branch(self, repo: Path) -> str | None: ...

    def branch_exists(self, repo: Path, name: str) -> bool: ...

    def create_branch(self, repo: Path, name: str, base: str) -> None:
        """base から name を作成して checkout する。"""
        ...

    def checkout(self, repo: Path, name: str) -> None: ...

    def commit_all(self, repo: Path, message: str) -> str | None:
        """未コミットの変更をすべてコミットする。変更がなければ None を返す。"""
        ...

    def save_to_branch(self, repo: Path, name: str) -> None:
        """現在の HEAD を name というブランチで保存する（既存なら上書き）。"""
        ...

    def reset_hard(self, repo: Path, sha: str) -> None: ...

    def diff_stat(self, repo: Path, base: str, head: str) -> str: ...

    def commits_between(self, repo: Path, base: str, head: str) -> list[str]: ...
