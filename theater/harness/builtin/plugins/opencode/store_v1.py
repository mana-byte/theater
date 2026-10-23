"""Read-only OpenCode 1.x message and part queries."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

SESSION_TABLE = "session"


def latest_message(conn: sqlite3.Connection, sid: str) -> tuple[object, ...] | None:
    return conn.execute(
        "SELECT data FROM message WHERE session_id = ? ORDER BY time_created DESC LIMIT 1", (sid,)
    ).fetchone()


def message_role(conn: sqlite3.Connection, message_id: str) -> tuple[object, ...] | None:
    return conn.execute("SELECT data FROM message WHERE id = ?", (message_id,)).fetchone()


def message_parts(conn: sqlite3.Connection, message_id: str):
    """The current part rows of one message, oldest first."""
    return conn.execute(
        "SELECT data FROM part WHERE message_id = ? ORDER BY time_created, id", (message_id,)
    )


def message_coordinate(conn: sqlite3.Connection, message_id: str) -> tuple[object, ...] | None:
    return conn.execute("SELECT time_created FROM message WHERE id = ?", (message_id,)).fetchone()


def part_ordinal(conn: sqlite3.Connection, part_id: str) -> int:
    """Match history's part ordering without reading or decoding sibling payloads."""
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


def recent_history_messages(conn: sqlite3.Connection, sid: str, *, limit: int, offset: int):
    return conn.execute(
        "SELECT id, data FROM message WHERE session_id = ? "
        "ORDER BY time_created DESC, id DESC LIMIT ? OFFSET ?",
        (sid, limit, offset),
    ).fetchall()


def history_parts_for_messages(conn: sqlite3.Connection, sid: str, message_ids: Sequence[str]):
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
    return conn.execute(
        "SELECT time_updated, data FROM message WHERE session_id = ? "
        "AND time_created = ? AND id = ?",
        (sid, created, message_id),
    ).fetchone()
