"""Read-only OpenCode SQLite access."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Literal, cast, overload

logger = logging.getLogger("theater.harness.opencode")


def open_readonly(db: Path, *, persistent: bool = False) -> sqlite3.Connection | None:
    if not db.exists():
        return None
    try:
        return sqlite3.connect(f"file:{db}?mode=ro", uri=True, check_same_thread=not persistent)
    except sqlite3.Error:
        logger.debug("opening %s failed", db, exc_info=True)
        return None


def root_session(conn: sqlite3.Connection, sid: str) -> tuple[str] | None:
    return cast(
        tuple[str] | None,
        conn.execute(
            "SELECT id FROM session WHERE id = ? AND parent_id IS NULL", (sid,)
        ).fetchone(),
    )


def session(conn: sqlite3.Connection, sid: str) -> tuple[str] | None:
    return cast(
        tuple[str] | None, conn.execute("SELECT id FROM session WHERE id = ?", (sid,)).fetchone()
    )


def candidate_sessions(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT id, directory, time_created FROM session "
        "WHERE parent_id IS NULL ORDER BY time_created DESC"
    )


def candidate_session(conn: sqlite3.Connection, sid: str) -> tuple[str, str, int | float] | None:
    return cast(
        tuple[str, str, int | float] | None,
        conn.execute(
            "SELECT id, directory, time_created FROM session WHERE id = ? AND parent_id IS NULL",
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
    sql = f"{select} FROM session WHERE directory = ? AND parent_id IS NULL"
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


def has_root_session(conn: sqlite3.Connection, directory: str, after: float | None) -> bool:
    sql = "SELECT 1 FROM session WHERE directory = ? AND parent_id IS NULL"
    args: list[object] = [directory]
    if after is not None:
        sql += " AND time_created >= ?"
        args.append(int(after * 1000))
    return conn.execute(sql + " LIMIT 1", args).fetchone() is not None


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
    return conn.execute(
        "SELECT data FROM message WHERE session_id = ? ORDER BY time_created DESC LIMIT 1", (sid,)
    ).fetchone()


def message_role(conn: sqlite3.Connection, message_id: str) -> tuple[object, ...] | None:
    return conn.execute("SELECT data FROM message WHERE id = ?", (message_id,)).fetchone()


def message_coordinate(conn: sqlite3.Connection, message_id: str) -> tuple[object, ...] | None:
    return conn.execute("SELECT time_created FROM message WHERE id = ?", (message_id,)).fetchone()


def live_revision_row(
    conn: sqlite3.Connection, table: str, record_id: str
) -> tuple[object, ...] | None:
    query = (
        "SELECT time_updated, time_created FROM message WHERE id = ?"
        if table == "message"
        else "SELECT time_updated, time_created FROM part WHERE id = ?"
    )
    return conn.execute(query, (record_id,)).fetchone()


def history_messages(conn: sqlite3.Connection, sid: str):
    return conn.execute(
        "SELECT id, data FROM message WHERE session_id = ? ORDER BY time_created, id", (sid,)
    )


def history_parts_by_session(conn: sqlite3.Connection, sid: str):
    return conn.execute(
        "SELECT message_id, data FROM part WHERE session_id = ? ORDER BY time_created, id", (sid,)
    )


def paged_messages(
    conn: sqlite3.Connection,
    sid: str,
    boundary: tuple[int | float, str, str] | None,
    limit: int,
):
    params: list[object] = [sid]
    sql = (
        "SELECT id, time_created, time_updated, data FROM message "
        "WHERE session_id = ? AND time_created IS NOT NULL"
    )
    if boundary is not None:
        created, message_id, _fingerprint = boundary
        sql += " AND (time_created < ? OR (time_created = ? AND id < ?))"
        params.extend((created, created, message_id))
    sql += " ORDER BY time_created DESC, id DESC LIMIT ?"
    params.append(limit + 1)
    return conn.execute(sql, params).fetchall()


def paged_parts(conn: sqlite3.Connection, sid: str, message_id: str, limit: int):
    return list(
        conn.execute(
            "SELECT id, time_created, time_updated, data FROM part "
            "WHERE message_id = ? AND session_id = ? "
            "ORDER BY time_created, id LIMIT ?",
            (message_id, sid, limit + 1),
        )
    )


def history_boundary(
    conn: sqlite3.Connection, sid: str, created: int | float, message_id: str
) -> tuple[object, ...] | None:
    return conn.execute(
        "SELECT time_updated, data FROM message WHERE session_id = ? "
        "AND time_created = ? AND id = ?",
        (sid, created, message_id),
    ).fetchone()
