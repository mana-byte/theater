"""Read-only OpenCode 2.x queries answered in the 1.x row shapes `store` callers expect.

Rows carry `seq` where 1.x carried `time_created`: it is the transcript order, and callers use
that column only as an ordering coordinate. 2.x keeps no event log, so live reads advance a
`(time_updated, seq)` cursor and re-read a short window for same-ms rewrites.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence

from . import translate_v2
from .values import load_json_object

#: The message types with a 1.x counterpart; the rest (instructions, switches, shell) are not
#: conversation, and an `idle` marker is kept so it can close its turn.
_VIEW = "type IN ('user', 'assistant', 'idle')"
_COLUMNS = "id, session_id, type, seq, time_updated, data"

type Row = tuple[str, str, str, int, int, str]


class V2Connection(sqlite3.Connection):
    """A read-only connection onto an OpenCode 2.x database."""


def is_v2(conn: sqlite3.Connection) -> bool:
    return isinstance(conn, V2Connection)


def row_info(conn: sqlite3.Connection, row: Row) -> dict:
    message_id, session_id, kind, seq, _updated, raw = row
    closes = kind == "idle" and _idle_closes_turn(conn, session_id, seq)
    return translate_v2.message_info(
        message_id, kind, session_id, load_json_object(raw), closes_turn=closes
    )


def row_parts(row: Row) -> list[dict]:
    message_id, session_id, kind, _seq, _updated, raw = row
    return translate_v2.message_parts(message_id, kind, session_id, load_json_object(raw))


def latest_message(conn: sqlite3.Connection, sid: str) -> tuple[object, ...] | None:
    row = latest_row(conn, sid)
    return None if row is None else (json.dumps(row_info(conn, row)),)


def latest_row(conn: sqlite3.Connection, sid: str) -> Row | None:
    return conn.execute(
        f"SELECT {_COLUMNS} FROM session_message WHERE session_id = ? AND {_VIEW} "
        "ORDER BY seq DESC LIMIT 1",
        (sid,),
    ).fetchone()


def message_role(conn: sqlite3.Connection, message_id: str) -> tuple[object, ...] | None:
    row = _row(conn, message_id)
    return None if row is None else (json.dumps(row_info(conn, row)),)


def message_parts(conn: sqlite3.Connection, message_id: str) -> list[tuple[str]]:
    row = _row(conn, message_id)
    return [] if row is None else [(json.dumps(part),) for part in row_parts(row)]


def message_coordinate(conn: sqlite3.Connection, message_id: str) -> tuple[object, ...] | None:
    return conn.execute("SELECT seq FROM session_message WHERE id = ?", (message_id,)).fetchone()


def part_ordinal(part_id: str) -> int:
    found = translate_v2.split_part_id(part_id)
    return found[1] if found is not None else 0


def live_revision_row(
    conn: sqlite3.Connection, table: str, record_id: str
) -> tuple[object, ...] | None:
    message_id: str | None = record_id
    if table == "part":
        found = translate_v2.split_part_id(record_id)
        message_id = found[0] if found is not None else None
    if message_id is None:
        return None
    return conn.execute(
        "SELECT time_updated, seq FROM session_message WHERE id = ?", (message_id,)
    ).fetchone()


def history_messages(conn: sqlite3.Connection, sid: str) -> list[tuple[str, str]]:
    return [(row[0], json.dumps(row_info(conn, row))) for row in _session_rows(conn, sid)]


def history_parts_by_session(conn: sqlite3.Connection, sid: str) -> list[tuple[str, str]]:
    return [
        (row[0], json.dumps(part)) for row in _session_rows(conn, sid) for part in row_parts(row)
    ]


def recent_history_messages(
    conn: sqlite3.Connection, sid: str, *, limit: int, offset: int
) -> list[tuple[str, str]]:
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM session_message WHERE session_id = ? AND {_VIEW} "
        "ORDER BY seq DESC LIMIT ? OFFSET ?",
        (sid, limit, offset),
    ).fetchall()
    return [(row[0], json.dumps(row_info(conn, row))) for row in rows]


def history_parts_for_messages(
    conn: sqlite3.Connection, sid: str, message_ids: Sequence[str]
) -> list[tuple[str, str]]:
    if not message_ids:
        return []
    placeholders = ",".join("?" for _ in message_ids)
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM session_message WHERE session_id = ? "
        f"AND id IN ({placeholders}) ORDER BY seq",
        (sid, *message_ids),
    ).fetchall()
    return [(row[0], json.dumps(part)) for row in rows for part in row_parts(row)]


def paged_messages(
    conn: sqlite3.Connection,
    sid: str,
    boundary: tuple[int | float, str, str] | None,
    limit: int,
    *,
    inclusive: bool = False,
) -> list[tuple[str, int, int, str]]:
    params: list[object] = [sid]
    sql = f"SELECT {_COLUMNS} FROM session_message WHERE session_id = ? AND {_VIEW}"
    if boundary is not None:
        seq, message_id, _fingerprint = boundary
        operator = "<=" if inclusive else "<"
        sql += f" AND (seq < ? OR (seq = ? AND id {operator} ?))"
        params.extend((seq, seq, message_id))
    sql += " ORDER BY seq DESC, id DESC LIMIT ?"
    params.append(limit + 1)
    rows = conn.execute(sql, params).fetchall()
    return [(row[0], row[3], row[4], json.dumps(row_info(conn, row))) for row in rows]


def paged_parts(
    conn: sqlite3.Connection, sid: str, message_id: str, limit: int | None
) -> list[tuple[str, int, int, str]]:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM session_message WHERE id = ? AND session_id = ?",
        (message_id, sid),
    ).fetchone()
    if row is None:
        return []
    parts = [(part["id"], row[3], row[4], json.dumps(part)) for part in row_parts(row)]
    return parts if limit is None else parts[: limit + 1]


def history_boundary(
    conn: sqlite3.Connection, sid: str, created: int | float, message_id: str
) -> tuple[object, ...] | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM session_message WHERE session_id = ? AND seq = ? AND id = ?",
        (sid, created, message_id),
    ).fetchone()
    return None if row is None else (row[4], json.dumps(row_info(conn, row)))


def head_point(conn: sqlite3.Connection, sid: str) -> tuple[int, int, int]:
    """The newest update instant, the highest seq at that instant, and the message count."""
    mark, count = conn.execute(
        "SELECT COALESCE(MAX(time_updated), 0), COUNT(*) FROM session_message "
        f"WHERE session_id = ? AND {_VIEW}",
        (sid,),
    ).fetchone()
    seq = conn.execute(
        f"SELECT COALESCE(MAX(seq), -1) FROM session_message WHERE session_id = ? AND {_VIEW} "
        "AND time_updated = ?",
        (sid, mark),
    ).fetchone()[0]
    return int(mark), int(seq), int(count)


def forward_rows(
    conn: sqlite3.Connection, sid: str, point: tuple[int, int], limit: int
) -> list[Row]:
    """Rows strictly after `point` ((time_updated, seq)), oldest update first."""
    updated, seq = point
    return conn.execute(
        f"SELECT {_COLUMNS} FROM session_message WHERE session_id = ? AND {_VIEW} "
        "AND (time_updated > ? OR (time_updated = ? AND seq > ?)) "
        "ORDER BY time_updated, seq LIMIT ?",
        (sid, updated, updated, seq, limit),
    ).fetchall()


def reread_rows(
    conn: sqlite3.Connection,
    sid: str,
    floor: int,
    boundary: tuple[int, int],
    limit: int,
    *,
    inclusive: bool = False,
) -> list[Row]:
    """One newest-first page of the window `[floor, boundary)` for same-ms rewrites."""
    updated, seq = boundary
    edge = "<=" if inclusive else "<"
    return conn.execute(
        f"SELECT {_COLUMNS} FROM session_message WHERE session_id = ? AND {_VIEW} "
        f"AND time_updated >= ? AND (time_updated < ? OR (time_updated = ? AND seq {edge} ?)) "
        "ORDER BY time_updated DESC, seq DESC LIMIT ?",
        (sid, floor, updated, updated, seq, limit),
    ).fetchall()


def open_turn_rows(conn: sqlite3.Connection, sid: str) -> list[Row]:
    """The rows after the newest idle marker: the turn still in flight, if any."""
    return conn.execute(
        f"SELECT {_COLUMNS} FROM session_message WHERE session_id = ? AND {_VIEW} AND seq > "
        "(SELECT COALESCE(MAX(seq), -1) FROM session_message WHERE session_id = ? "
        "AND type = 'idle') ORDER BY seq",
        (sid, sid),
    ).fetchall()


def _row(conn: sqlite3.Connection, message_id: str) -> Row | None:
    return conn.execute(
        f"SELECT {_COLUMNS} FROM session_message WHERE id = ? AND {_VIEW}", (message_id,)
    ).fetchone()


def _session_rows(conn: sqlite3.Connection, sid: str) -> list[Row]:
    return conn.execute(
        f"SELECT {_COLUMNS} FROM session_message WHERE session_id = ? AND {_VIEW} ORDER BY seq",
        (sid,),
    ).fetchall()


def _idle_closes_turn(conn: sqlite3.Connection, sid: str, seq: int) -> bool:
    previous = conn.execute(
        "SELECT COALESCE(MAX(seq), -1) FROM session_message "
        "WHERE session_id = ? AND type = 'idle' AND seq < ?",
        (sid, seq),
    ).fetchone()[0]
    assistant = conn.execute(
        "SELECT id, data FROM session_message WHERE session_id = ? AND type = 'assistant' "
        "AND seq > ? AND seq < ? ORDER BY seq DESC LIMIT 1",
        (sid, previous, seq),
    ).fetchone()
    if assistant is None:
        prompted = conn.execute(
            "SELECT 1 FROM session_message WHERE session_id = ? AND type = 'user' "
            "AND seq > ? AND seq < ? LIMIT 1",
            (sid, previous, seq),
        ).fetchone()
        return translate_v2.idle_closes_turn(None, prompted is not None)
    data = load_json_object(assistant[1])
    info = translate_v2.message_info(assistant[0], "assistant", sid, data)
    parts = translate_v2.message_parts(assistant[0], "assistant", sid, data)
    return translate_v2.idle_closes_turn((info, parts), True)
