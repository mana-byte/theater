"""Non-waiting WAL maintenance on a daemon-owned worker connection."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def passive_checkpoint(path: Path) -> tuple[int, int, int]:
    """Never wait for readers or share the event loop's SQLite connection."""
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True, timeout=0)
    try:
        row = connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        return int(row[0]), int(row[1]), int(row[2])
    except sqlite3.OperationalError as exc:
        if exc.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return 1, -1, -1
        raise
    finally:
        connection.close()
