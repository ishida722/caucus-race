"""SQLite によるリポジトリ実装。

エンティティは `data` 列に JSON で丸ごと保存し、絞り込みに使う属性だけを列として持つ。
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Column,
    Connection,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
    insert,
    select,
    update,
)

from caucus_race.domain.entities import (
    Entity,
    Event,
    Feedback,
    Project,
    RetroNote,
    Spike,
    Sprint,
    Story,
    Task,
    now,
)
from caucus_race.domain.errors import NotFoundError

FILTER_COLUMNS = ("project_id", "sprint_id", "story_id", "status", "name")

TABLE_NAMES: dict[type[Entity], str] = {
    Project: "projects",
    Sprint: "sprints",
    Story: "stories",
    Task: "tasks",
    Spike: "spikes",
    Feedback: "feedbacks",
    RetroNote: "retro_notes",
    Event: "events",
}

metadata = MetaData()

counters = Table(
    "counters",
    metadata,
    Column("prefix", String, primary_key=True),
    Column("value", Integer, nullable=False),
)


def _entity_table(name: str) -> Table:
    return Table(
        name,
        metadata,
        Column("seq", Integer, primary_key=True, autoincrement=True),
        Column("id", String, nullable=False, unique=True),
        *(Column(c, String, nullable=True, index=True) for c in FILTER_COLUMNS),
        Column("data", Text, nullable=False),
    )


TABLES: dict[type[Entity], Table] = {t: _entity_table(n) for t, n in TABLE_NAMES.items()}


def _row_values(entity: Entity) -> dict[str, Any]:
    values: dict[str, Any] = {"id": entity.id, "data": entity.model_dump_json()}
    for col in FILTER_COLUMNS:
        value = getattr(entity, col, None)
        values[col] = None if value is None else str(value)
    return values


class SqliteRepository:
    def __init__(self, db_path: Path | str) -> None:
        url = "sqlite://" if str(db_path) == ":memory:" else f"sqlite:///{db_path}"
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(url)

        @event.listens_for(self._engine, "connect")
        def _pragma(dbapi_conn: Any, _: Any) -> None:
            dbapi_conn.execute("PRAGMA journal_mode=WAL")

        metadata.create_all(self._engine)
        self._conn: Connection | None = None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._conn is not None:
            yield
            return
        with self._engine.begin() as conn:
            self._conn = conn
            try:
                yield
            finally:
                self._conn = None

    @contextmanager
    def _connection(self) -> Iterator[Connection]:
        with self.transaction():
            assert self._conn is not None
            yield self._conn

    def next_id[E: Entity](self, entity_type: type[E]) -> str:
        prefix = entity_type.id_prefix
        with self._connection() as conn:
            current = conn.execute(
                select(counters.c.value).where(counters.c.prefix == prefix)
            ).scalar_one_or_none()
            if current is None:
                value = 1
                conn.execute(insert(counters).values(prefix=prefix, value=value))
            else:
                value = current + 1
                conn.execute(update(counters).where(counters.c.prefix == prefix).values(value=value))
        return f"{prefix}-{value}"

    def add[E: Entity](self, entity: E) -> E:
        with self._connection() as conn:
            conn.execute(insert(TABLES[type(entity)]).values(**_row_values(entity)))
        return entity

    def save[E: Entity](self, entity: E) -> E:
        entity.updated_at = now()
        table = TABLES[type(entity)]
        with self._connection() as conn:
            result = conn.execute(update(table).where(table.c.id == entity.id).values(**_row_values(entity)))
            if result.rowcount == 0:
                raise NotFoundError(f"{entity.id} が見つかりません")
        return entity

    def find[E: Entity](self, entity_type: type[E], entity_id: str) -> E | None:
        table = TABLES[entity_type]
        with self._connection() as conn:
            data = conn.execute(select(table.c.data).where(table.c.id == entity_id)).scalar_one_or_none()
        return None if data is None else entity_type.model_validate_json(data)

    def get[E: Entity](self, entity_type: type[E], entity_id: str) -> E:
        entity = self.find(entity_type, entity_id)
        if entity is None:
            raise NotFoundError(f"{entity_id} が見つかりません")
        return entity

    def query[E: Entity](self, entity_type: type[E], **filters: Any) -> list[E]:
        table = TABLES[entity_type]
        stmt = select(table.c.data).order_by(table.c.seq)
        for key, value in filters.items():
            if key not in FILTER_COLUMNS:
                raise ValueError(f"{key} では絞り込めません")
            column = table.c[key]
            if value is None:
                stmt = stmt.where(column.is_(None))
            elif isinstance(value, (list, tuple, set, frozenset)):
                stmt = stmt.where(column.in_([str(v) for v in value]))
            else:
                stmt = stmt.where(column == str(value))
        with self._connection() as conn:
            rows = conn.execute(stmt).scalars().all()
        return [entity_type.model_validate_json(r) for r in rows]

    def iter_all[E: Entity](self, entity_type: type[E]) -> Iterator[E]:
        yield from self.query(entity_type)
