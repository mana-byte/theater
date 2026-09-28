"""Read-only OpenCode SQLite access.

A 2.x database opens as a `V2Connection`, and every query below answers it from `store_v2`
in the same 1.x row shapes, so the parser, history, and trajectory read both releases.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, cast, overload

from . import store_v2
from .dialect import is_v2_database
from .store_v2 import V2Connection, is_v2

logger = logging.getLogger("theater.harness.opencode")


def connect(db: Path, *, persistent: bool = False) -> sqlite3.Connection:
    factory = V2Connection if is_v2_database(db) else sqlite3.Connection
    return sqlite3.connect(
        f"file:{db}?mode=ro", uri=True, check_same_thread=not persistent, factory=factory
    )


def open_readonly(db: Path, *, persistent: bool = False) -> sqlite3.Connection | None:
    if not db.exists():
        return None
    try:
        return connect(db, persistent=persistent)
    except sqlite3.Error:
        logger.debug("opening %s failed", db, exc_info=True)
        return None


def _sessions(conn: sqlite3.Connection) -> str:
    return "session_v2" if is_v2(conn) else "session"


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


def latest_message(conn: sqlite3.Connection, sid: str) -> tuple[object, ...] | None:
    if is_v2(conn):
        return store_v2.latest_message(conn, sid)
    return conn.execute(
        "SELECT data FROM message WHERE session_id = ? ORDER BY time_created DESC LIMIT 1", (sid,)
    ).fetchone()


def message_role(conn: sqlite3.Connection, message_id: str) -> tuple[object, ...] | None:
    if is_v2(conn):
        return store_v2.message_role(conn, message_id)
    return conn.execute("SELECT data FROM message WHERE id = ?", (message_id,)).fetchone()


def message_parts(conn: sqlite3.Connection, message_id: str):
    """The current part rows of one message, oldest first.

    Terminal classification needs live parts: native asks `hasToolCalls`
    (session/prompt.ts:1097-1115).
    """
    if is_v2(conn):
        return store_v2.message_parts(conn, message_id)
    return conn.execute(
        "SELECT data FROM part WHERE message_id = ? ORDER BY time_created, id", (message_id,)
    )


def message_coordinate(conn: sqlite3.Connection, message_id: str) -> tuple[object, ...] | None:
    if is_v2(conn):
        return store_v2.message_coordinate(conn, message_id)
    return conn.execute("SELECT time_created FROM message WHERE id = ?", (message_id,)).fetchone()


def part_ordinal(conn: sqlite3.Connection, part_id: str) -> int:
    """Match history's part ordering without reading or decoding sibling payloads."""
    if is_v2(conn):
        return store_v2.part_ordinal(part_id)
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM part AS earlier "
        "WHERE earlier.message_id = current.message_id "
        "AND (earlier.time_created, earlier.id) < (current.time_created, current.id)) "
        "FROM part AS current WHERE current.id = ?",
        (part_id,),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def live_revision_row(
    conn: sqlite3.Connection, table: str, record_id: str
) -> tuple[object, ...] | None:
    if is_v2(conn):
        return store_v2.live_revision_row(conn, table, record_id)
    query = (
        "SELECT time_updated, time_created FROM message WHERE id = ?"
        if table == "message"
        else "SELECT time_updated, time_created FROM part WHERE id = ?"
    )
    return conn.execute(query, (record_id,)).fetchone()


def history_messages(conn: sqlite3.Connection, sid: str):
    if is_v2(conn):
        return store_v2.history_messages(conn, sid)
    return conn.execute(
        "SELECT id, data FROM message WHERE session_id = ? ORDER BY time_created, id", (sid,)
    )


def history_parts_by_session(conn: sqlite3.Connection, sid: str):
    if is_v2(conn):
        return store_v2.history_parts_by_session(conn, sid)
    return conn.execute(
        "SELECT message_id, data FROM part WHERE session_id = ? ORDER BY time_created, id", (sid,)
    )


def recent_history_messages(
    conn: sqlite3.Connection,
    sid: str,
    *,
    limit: int,
    offset: int,
):
    if is_v2(conn):
        return store_v2.recent_history_messages(conn, sid, limit=limit, offset=offset)
    return conn.execute(
        "SELECT id, data FROM message WHERE session_id = ? "
        "ORDER BY time_created DESC, id DESC LIMIT ? OFFSET ?",
        (sid, limit, offset),
    ).fetchall()


def history_parts_for_messages(
    conn: sqlite3.Connection,
    sid: str,
    message_ids: Sequence[str],
):
    if is_v2(conn):
        return store_v2.history_parts_for_messages(conn, sid, message_ids)
    if not message_ids:
        return []
    placeholders = ",".join("?" for _ in message_ids)
    return conn.execute(
        f"SELECT message_id, data FROM part WHERE session_id = ? "
        f"AND message_id IN ({placeholders}) ORDER BY time_created, id",
        (sid, *message_ids),
    ).fetchall()


def paged_messages(
    conn: sqlite3.Connection,
    sid: str,
    boundary: tuple[int | float, str, str] | None,
    limit: int,
    *,
    inclusive: bool = False,
):
    if is_v2(conn):
        return store_v2.paged_messages(conn, sid, boundary, limit, inclusive=inclusive)
    params: list[object] = [sid]
    sql = (
        "SELECT id, time_created, time_updated, data FROM message "
        "WHERE session_id = ? AND time_created IS NOT NULL"
    )
    if boundary is not None:
        created, message_id, _fingerprint = boundary
        operator = "<=" if inclusive else "<"
        sql += f" AND (time_created < ? OR (time_created = ? AND id {operator} ?))"
        params.extend((created, created, message_id))
    sql += " ORDER BY time_created DESC, id DESC LIMIT ?"
    params.append(limit + 1)
    return conn.execute(sql, params).fetchall()


def paged_parts(conn: sqlite3.Connection, sid: str, message_id: str, limit: int | None):
    if is_v2(conn):
        return store_v2.paged_parts(conn, sid, message_id, limit)
    query = (
        "SELECT id, time_created, time_updated, data FROM part "
        "WHERE message_id = ? AND session_id = ? ORDER BY time_created, id"
    )
    params: tuple[object, ...] = (message_id, sid)
    if limit is not None:
        query += " LIMIT ?"
        params += (limit + 1,)
    return list(conn.execute(query, params))


def history_boundary(
    conn: sqlite3.Connection, sid: str, created: int | float, message_id: str
) -> tuple[object, ...] | None:
    if is_v2(conn):
        return store_v2.history_boundary(conn, sid, created, message_id)
    return conn.execute(
        "SELECT time_updated, data FROM message WHERE session_id = ? "
        "AND time_created = ? AND id = ?",
        (sid, created, message_id),
    ).fetchone()
