"""Read-only OpenCode queries answered in the normalized row shapes `store` callers expect.

Rows carry `seq` as the transcript order, and callers use that column only as an ordering
coordinate. OpenCode keeps no event log, so live reads advance a
`(time_updated, seq)` cursor and re-read a short window for same-ms rewrites.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence

from . import compaction, translate
from .values import load_json_object

#: The message types observed as conversation; the rest (instructions, switches, shell) are not
#: conversation, and an `idle` marker is kept so it can close its turn. A `compaction` row is
#: paid usage, not talk, so it rides the same cursor the observer drains.
_VIEW = "type IN ('user', 'assistant', 'idle', 'compaction')"
#: Status answers "is a turn in flight"; a compaction never is one, so it stays out.
_TURN_VIEW = "type IN ('user', 'assistant', 'idle')"
_COLUMNS = "id, session_id, type, seq, time_updated, data"
#: Effective row update timestamp when assistant parts live in `session_part` (OpenCode 2.4+).
_EFFECTIVE_UPDATED = (
    "MAX(session_message.time_updated, "
    "COALESCE((SELECT MAX(p.time_updated) FROM session_part p "
    "WHERE p.message_id = session_message.id), 0))"
)
_PART_BATCH_SIZE = 500
SESSION_TABLE = "session_v2"

type Row = tuple[str, str, str, int, int, str]


def _has_part_table(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'session_part'"
        ).fetchone()
        is not None
    )


def _updated_expr(conn: sqlite3.Connection) -> str:
    return _EFFECTIVE_UPDATED if _has_part_table(conn) else "time_updated"


def _with_parts(conn: sqlite3.Connection, rows: list[Row]) -> list[Row]:
    """Fold `session_part` rows into assistant `data.content` in ordinal order."""
    if not rows or not _has_part_table(conn):
        return rows
    assistant_ids = [row[0] for row in rows if row[2] == "assistant"]
    if not assistant_ids:
        return rows
    parts_by_message: dict[str, list[object]] = {}
    max_updated: dict[str, int] = {}
    for start in range(0, len(assistant_ids), _PART_BATCH_SIZE):
        batch = assistant_ids[start : start + _PART_BATCH_SIZE]
        placeholders = ",".join("?" for _ in batch)
        for message_id, updated, raw in conn.execute(
            "SELECT message_id, time_updated, data FROM session_part "
            f"WHERE message_id IN ({placeholders}) ORDER BY message_id, ordinal",
            batch,
        ).fetchall():
            try:
                part = json.loads(raw)
            except (TypeError, ValueError):
                continue
            parts_by_message.setdefault(message_id, []).append(part)
            prev = max_updated.get(message_id, 0)
            if isinstance(updated, int) and updated > prev:
                max_updated[message_id] = updated
    if not parts_by_message:
        return rows
    merged: list[Row] = []
    for row in rows:
        parts = parts_by_message.get(row[0])
        if not parts:
            merged.append(row)
            continue
        data = load_json_object(row[5])
        inline = data.get("content")
        data["content"] = [*parts, *inline] if isinstance(inline, list) else parts
        updated = max(row[4], max_updated[row[0]])
        merged.append((row[0], row[1], row[2], row[3], updated, json.dumps(data)))
    return merged


def _with_parts_one(conn: sqlite3.Connection, row: Row | None) -> Row | None:
    return None if row is None else _with_parts(conn, [row])[0]


def row_info(conn: sqlite3.Connection, row: Row) -> dict:
    message_id, session_id, kind, seq, _updated, raw = row
    if kind == "compaction":
        return compaction.message_info(message_id, session_id, load_json_object(raw))
    closes, cause = _idle_close(conn, session_id, seq) if kind == "idle" else (False, None)
    return translate.message_info(
        message_id, kind, session_id, load_json_object(raw), closes_turn=closes, cause=cause
    )


def row_parts(row: Row) -> list[dict]:
    message_id, session_id, kind, _seq, _updated, raw = row
    return translate.message_parts(message_id, kind, session_id, load_json_object(raw))


def latest_row(conn: sqlite3.Connection, sid: str) -> Row | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM session_message WHERE session_id = ? AND {_TURN_VIEW} "
        "ORDER BY seq DESC LIMIT 1",
        (sid,),
    ).fetchone()
    return _with_parts_one(conn, row)


def message_role(conn: sqlite3.Connection, message_id: str) -> tuple[object, ...] | None:
    row = _row(conn, message_id)
    return None if row is None else (json.dumps(row_info(conn, row)),)


def message_parts(conn: sqlite3.Connection, message_id: str) -> list[tuple[str]]:
    row = _row(conn, message_id)
    return [] if row is None else [(json.dumps(part),) for part in row_parts(row)]


def message_coordinate(conn: sqlite3.Connection, message_id: str) -> tuple[object, ...] | None:
    return conn.execute("SELECT seq FROM session_message WHERE id = ?", (message_id,)).fetchone()


def part_ordinal(_conn: sqlite3.Connection, part_id: str) -> int:
    found = translate.split_part_id(part_id)
    return found[1] if found is not None else 0


def live_revision_row(
    conn: sqlite3.Connection, table: str, record_id: str
) -> tuple[object, ...] | None:
    message_id: str | None = record_id
    if table == "part":
        found = translate.split_part_id(record_id)
        message_id = found[0] if found is not None else None
    if message_id is None:
        return None
    updated = _updated_expr(conn)
    return conn.execute(
        f"SELECT {updated}, seq FROM session_message WHERE id = ?", (message_id,)
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
    rows = _with_parts(
        conn,
        conn.execute(
            f"SELECT {_COLUMNS} FROM session_message WHERE session_id = ? "
            f"AND id IN ({placeholders}) ORDER BY seq",
            (sid, *message_ids),
        ).fetchall(),
    )
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
    row = _with_parts_one(
        conn,
        conn.execute(
            f"SELECT {_COLUMNS} FROM session_message WHERE id = ? AND session_id = ?",
            (message_id, sid),
        ).fetchone(),
    )
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
    updated = _updated_expr(conn)
    mark, count = conn.execute(
        f"SELECT COALESCE(MAX({updated}), 0), COUNT(*) FROM session_message "
        f"WHERE session_id = ? AND {_VIEW}",
        (sid,),
    ).fetchone()
    seq = conn.execute(
        f"SELECT COALESCE(MAX(seq), -1) FROM session_message WHERE session_id = ? AND {_VIEW} "
        f"AND {updated} = ?",
        (sid, mark),
    ).fetchone()[0]
    return int(mark), int(seq), int(count)


def forward_rows(
    conn: sqlite3.Connection, sid: str, point: tuple[int, int], limit: int
) -> list[Row]:
    """Rows strictly after `point` ((time_updated, seq)), oldest update first."""
    updated_val, seq = point
    updated = _updated_expr(conn)
    rows = conn.execute(
        f"SELECT id, session_id, type, seq, {updated} AS eff_updated, data "
        f"FROM session_message WHERE session_id = ? AND {_VIEW} "
        f"AND ({updated} > ? OR ({updated} = ? AND seq > ?)) "
        f"ORDER BY {updated}, seq LIMIT ?",
        (sid, updated_val, updated_val, seq, limit),
    ).fetchall()
    return _with_parts(conn, rows)


def reread_rows(
    conn: sqlite3.Connection,
    sid: str,
    floor: int,
    cursor: tuple[int, int],
    boundary: tuple[int, int],
    limit: int,
) -> list[Row]:
    """One oldest-first page of the window: rows in (cursor, boundary] updated at/after floor."""
    cu, cs = cursor
    bu, bs = boundary
    updated = _updated_expr(conn)
    rows = conn.execute(
        f"SELECT id, session_id, type, seq, {updated} AS eff_updated, data "
        f"FROM session_message WHERE session_id = ? AND {_VIEW} "
        f"AND {updated} >= ? "
        f"AND ({updated} > ? OR ({updated} = ? AND seq > ?)) "
        f"AND ({updated} < ? OR ({updated} = ? AND seq <= ?)) "
        f"ORDER BY {updated}, seq LIMIT ?",
        (sid, floor, cu, cu, cs, bu, bu, bs, limit),
    ).fetchall()
    return _with_parts(conn, rows)


def open_turn_rows(conn: sqlite3.Connection, sid: str, *, after_seq: int, limit: int) -> list[Row]:
    """One page after the newest idle marker: the turn still in flight, if any."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM session_message WHERE session_id = ? AND {_VIEW} AND seq > "
        "(SELECT COALESCE(MAX(seq), -1) FROM session_message WHERE session_id = ? "
        "AND type = 'idle') AND seq > ? ORDER BY seq LIMIT ?",
        (sid, sid, after_seq, limit),
    ).fetchall()
    return _with_parts(conn, rows)


def _row(conn: sqlite3.Connection, message_id: str) -> Row | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM session_message WHERE id = ? AND {_VIEW}", (message_id,)
    ).fetchone()
    return _with_parts_one(conn, row)


def _session_rows(conn: sqlite3.Connection, sid: str) -> list[Row]:
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM session_message WHERE session_id = ? AND {_VIEW} ORDER BY seq",
        (sid,),
    ).fetchall()
    return _with_parts(conn, rows)


def _idle_close(conn: sqlite3.Connection, sid: str, seq: int) -> tuple[bool, dict | None]:
    """Whether an idle marker closes its turn, and the last step failure that names why."""
    previous = conn.execute(
        "SELECT COALESCE(MAX(seq), -1) FROM session_message "
        "WHERE session_id = ? AND type = 'idle' AND seq < ?",
        (sid, seq),
    ).fetchone()[0]
    assistant = _with_parts_one(
        conn,
        conn.execute(
            f"SELECT {_COLUMNS} FROM session_message WHERE session_id = ? AND type = 'assistant' "
            "AND seq > ? AND seq < ? ORDER BY seq DESC LIMIT 1",
            (sid, previous, seq),
        ).fetchone(),
    )
    if assistant is None:
        prompted = conn.execute(
            "SELECT 1 FROM session_message WHERE session_id = ? AND type = 'user' "
            "AND seq > ? AND seq < ? LIMIT 1",
            (sid, previous, seq),
        ).fetchone()
        return translate.idle_closes_turn(None, prompted is not None), None
    data = load_json_object(assistant[5])
    info = translate.message_info(assistant[0], "assistant", sid, data)
    parts = translate.message_parts(assistant[0], "assistant", sid, data)
    return translate.idle_closes_turn((info, parts), True), info.get("stepError")
