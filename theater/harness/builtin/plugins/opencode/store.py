"""Read-only OpenCode SQLite access with a query dialect bound when opened."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Literal, Protocol, cast, overload

from . import store_v1, store_v2
from .dialect import is_v2_database

logger = logging.getLogger("theater.harness.opencode")


class QueryNamespace(Protocol):
    SESSION_TABLE: str

    def latest_message(self, conn: sqlite3.Connection, sid: str) -> tuple[object, ...] | None: ...
    def message_role(
        self, conn: sqlite3.Connection, message_id: str
    ) -> tuple[object, ...] | None: ...
    def message_parts(self, conn: sqlite3.Connection, message_id: str): ...
    def message_coordinate(
        self, conn: sqlite3.Connection, message_id: str
    ) -> tuple[object, ...] | None: ...
    def part_ordinal(self, conn: sqlite3.Connection, part_id: str) -> int: ...
    def live_revision_row(
        self, conn: sqlite3.Connection, table: str, record_id: str
    ) -> tuple[object, ...] | None: ...
    def history_messages(self, conn: sqlite3.Connection, sid: str): ...
    def history_parts_by_session(self, conn: sqlite3.Connection, sid: str): ...
    def recent_history_messages(
        self, conn: sqlite3.Connection, sid: str, *, limit: int, offset: int
    ): ...
    def history_parts_for_messages(
        self, conn: sqlite3.Connection, sid: str, message_ids: list[str]
    ): ...
    def paged_messages(
        self,
        conn: sqlite3.Connection,
        sid: str,
        boundary: tuple[int | float, str, str] | None,
        limit: int,
        *,
        inclusive: bool = False,
    ): ...
    def paged_parts(
        self, conn: sqlite3.Connection, sid: str, message_id: str, limit: int | None
    ): ...
    def history_boundary(
        self, conn: sqlite3.Connection, sid: str, created: int | float, message_id: str
    ) -> tuple[object, ...] | None: ...


class QueryConnection(sqlite3.Connection):
    queries: QueryNamespace


def select_queries(db: Path) -> QueryNamespace:
    return store_v2 if is_v2_database(db) else store_v1


def connect(
    db: Path, *, persistent: bool = False, queries: QueryNamespace | None = None
) -> QueryConnection:
    conn = cast(
        QueryConnection,
        sqlite3.connect(
            f"file:{db}?mode=ro",
            uri=True,
            check_same_thread=not persistent,
            factory=QueryConnection,
        ),
    )
    conn.queries = queries if queries is not None else select_queries(db)
    return conn


def open_readonly(
    db: Path, *, persistent: bool = False, queries: QueryNamespace | None = None
) -> QueryConnection | None:
    if not db.exists():
        return None
    try:
        return connect(db, persistent=persistent, queries=queries)
    except sqlite3.Error:
        logger.debug("opening %s failed", db, exc_info=True)
        return None


def _sessions(conn: sqlite3.Connection) -> str:
    return cast(QueryConnection, conn).queries.SESSION_TABLE


def root_session(conn: sqlite3.Connection, sid: str) -> tuple[str] | None:
    return cast(
        tuple[str] | None,
        conn.execute(
            f"SELECT id FROM {_sessions(conn)} WHERE id = ? AND parent_id IS NULL", (sid,)
        ).fetchone(),
    )


def session(conn: sqlite3.Connection, sid: str) -> tuple[str] | None:
    return cast(
        tuple[str] | None,
        conn.execute(f"SELECT id FROM {_sessions(conn)} WHERE id = ?", (sid,)).fetchone(),
    )


def candidate_sessions(conn: sqlite3.Connection):
    return conn.execute(
        f"SELECT id, directory, time_created FROM {_sessions(conn)} "
        "WHERE parent_id IS NULL ORDER BY time_created DESC"
    )


def candidate_session(conn: sqlite3.Connection, sid: str) -> tuple[str, str, int | float] | None:
    return cast(
        tuple[str, str, int | float] | None,
        conn.execute(
            f"SELECT id, directory, time_created FROM {_sessions(conn)} "
            "WHERE id = ? AND parent_id IS NULL",
            (sid,),
        ).fetchone(),
    )


@overload
def located_sessions(
    conn: sqlite3.Connection, directory: str, after: float | None, *, count: Literal[True]
) -> tuple[int] | None: ...


@overload
def located_sessions(
    conn: sqlite3.Connection, directory: str, after: float | None, *, count: Literal[False] = False
) -> tuple[str] | None: ...


def located_sessions(
    conn: sqlite3.Connection, directory: str, after: float | None, *, count: bool = False
) -> tuple[int] | tuple[str] | None:
    select = "SELECT COUNT(*)" if count else "SELECT id"
    sql = f"{select} FROM {_sessions(conn)} WHERE directory = ? AND parent_id IS NULL"
    args: list[object] = [directory]
    if after is not None:
        sql += " AND time_created >= ?"
        args.append(int(after * 1000))
    if not count:
        sql += " ORDER BY time_created DESC LIMIT 1"
    return cast(tuple[int] | tuple[str] | None, conn.execute(sql, args).fetchone())


def session_for_history(
    conn: sqlite3.Connection, directory: str, after: float | None
) -> tuple[str] | None:
    return located_sessions(conn, directory, after)


def event_head(conn: sqlite3.Connection, sid: str) -> tuple[int, int]:
    return cast(
        tuple[int, int],
        conn.execute(
            "SELECT COALESCE(MAX(seq), -1), COUNT(*) FROM event WHERE aggregate_id = ?", (sid,)
        ).fetchone(),
    )


def event_rows(conn: sqlite3.Connection, sid: str | None, cursor: int, limit: int):
    return conn.execute(
        "SELECT seq, type, data FROM event WHERE aggregate_id = ? AND seq > ? ORDER BY seq LIMIT ?",
        (sid, cursor, limit),
    ).fetchall()


def history_messages(conn: sqlite3.Connection, sid: str):
    """Compatibility entry point used by the selected-server proof."""
    return cast(QueryConnection, conn).queries.history_messages(conn, sid)
