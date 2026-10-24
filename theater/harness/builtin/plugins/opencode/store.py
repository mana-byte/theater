"""Read-only access to the supported OpenCode SQLite schema."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Literal, cast, overload

from .store_v2 import SESSION_TABLE

logger = logging.getLogger("theater.harness.opencode")


def connect(db: Path, *, persistent: bool = False) -> sqlite3.Connection:
    return sqlite3.connect(
        f"file:{db}?mode=ro",
        uri=True,
        check_same_thread=not persistent,
    )


def open_readonly(db: Path, *, persistent: bool = False) -> sqlite3.Connection | None:
    if not db.exists():
        return None
    try:
        return connect(db, persistent=persistent)
    except sqlite3.Error:
        logger.debug("opening %s failed", db, exc_info=True)
        return None


def root_session(conn: sqlite3.Connection, sid: str) -> tuple[str] | None:
    return cast(
        tuple[str] | None,
        conn.execute(
            f"SELECT id FROM {SESSION_TABLE} WHERE id = ? AND parent_id IS NULL", (sid,)
        ).fetchone(),
    )


def session(conn: sqlite3.Connection, sid: str) -> tuple[str] | None:
    return cast(
        tuple[str] | None,
        conn.execute(f"SELECT id FROM {SESSION_TABLE} WHERE id = ?", (sid,)).fetchone(),
    )


def candidate_sessions(conn: sqlite3.Connection):
    return conn.execute(
        f"SELECT id, directory, time_created FROM {SESSION_TABLE} "
        "WHERE parent_id IS NULL ORDER BY time_created DESC"
    )


def candidate_session(conn: sqlite3.Connection, sid: str) -> tuple[str, str, int | float] | None:
    return cast(
        tuple[str, str, int | float] | None,
        conn.execute(
            f"SELECT id, directory, time_created FROM {SESSION_TABLE} "
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
    sql = f"{select} FROM {SESSION_TABLE} WHERE directory = ? AND parent_id IS NULL"
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
