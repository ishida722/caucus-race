from collections.abc import Iterator
from contextlib import AbstractContextManager
from typing import Any, Protocol

from caucus_race.domain.entities import Entity


class Repository(Protocol):
    """エンティティの永続化。

    `query` の絞り込みに使える属性は project_id / sprint_id / story_id / status / name。
    """

    def transaction(self) -> AbstractContextManager[None]:
        """入れ子で呼ばれた場合は外側のトランザクションに合流する。"""
        ...

    def next_id[E: Entity](self, entity_type: type[E]) -> str: ...

    def add[E: Entity](self, entity: E) -> E: ...

    def save[E: Entity](self, entity: E) -> E: ...

    def get[E: Entity](self, entity_type: type[E], entity_id: str) -> E: ...

    def find[E: Entity](self, entity_type: type[E], entity_id: str) -> E | None: ...

    def query[E: Entity](self, entity_type: type[E], **filters: Any) -> list[E]: ...

    def iter_all[E: Entity](self, entity_type: type[E]) -> Iterator[E]: ...
