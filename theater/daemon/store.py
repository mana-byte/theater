"""SQLite persistence.

Deliberately synchronous. Calls are local, sub-millisecond, and bounded by the
number of participants (tens, not thousands), so running them on the event loop
is cheaper than the complexity of an async driver. Revisit if that stops being
true.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from theater.models import Job, Participant, Status, now

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
    id            TEXT PRIMARY KEY,
    harness       TEXT NOT NULL,
    tier          TEXT NOT NULL,
    tmux_pane     TEXT,
    cwd           TEXT,
    branch        TEXT,
    session_id    TEXT,
    parent_id     TEXT,
    pid           INTEGER,
    status        TEXT NOT NULL,
    last_activity REAL NOT NULL,
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_participants_pane   ON participants(tmux_pane);
CREATE INDEX IF NOT EXISTS idx_participants_parent ON participants(parent_id);
CREATE INDEX IF NOT EXISTS idx_participants_status ON participants(status);

-- Populated in phase 5a. Created now so there is no migration later.
CREATE TABLE IF NOT EXISTS jobs (
    handle      TEXT PRIMARY KEY,
    caller_id   TEXT NOT NULL,
    target_id   TEXT,
    kind        TEXT NOT NULL,
    prompt      TEXT,
    state       TEXT NOT NULL,
    result      TEXT,
    error_code  TEXT,
    created_at  REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_caller ON jobs(caller_id);
CREATE INDEX IF NOT EXISTS idx_jobs_state  ON jobs(state);

CREATE TABLE IF NOT EXISTS bus (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    from_id TEXT,
    to_id   TEXT,
    kind    TEXT NOT NULL,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_bus_ts ON bus(ts);

CREATE TABLE IF NOT EXISTS budgets (
    tree_root_id TEXT PRIMARY KEY,
    tokens       INTEGER NOT NULL DEFAULT 0,
    cents        INTEGER NOT NULL DEFAULT 0,
    limit_cents  INTEGER
);
"""

_FIELDS = (
    "id", "harness", "tier", "tmux_pane", "cwd", "branch", "session_id",
    "parent_id", "pid", "status", "last_activity", "created_at",
)


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        current = self.db.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"database at {self.path} has schema v{current}, "
                f"this build understands v{SCHEMA_VERSION}"
            )
        self.db.executescript(SCHEMA)
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        self.db.close()

    # ---- participants -------------------------------------------------

    def upsert_participant(self, p: Participant) -> None:
        cols = ", ".join(_FIELDS)
        marks = ", ".join("?" for _ in _FIELDS)
        updates = ", ".join(f"{f}=excluded.{f}" for f in _FIELDS if f != "id")
        values = [
            p.id, p.harness, str(p.tier), p.tmux_pane, p.cwd, p.branch,
            p.session_id, p.parent_id, p.pid, str(p.status),
            p.last_activity, p.created_at,
        ]
        self.db.execute(
            f"INSERT INTO participants ({cols}) VALUES ({marks}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            values,
        )

    def get_participant(self, pid: str) -> Participant | None:
        row = self.db.execute(
            "SELECT * FROM participants WHERE id = ?", (pid,)
        ).fetchone()
        return Participant.from_row(row) if row else None

    def find_by_pane(self, pane: str) -> Participant | None:
        row = self.db.execute(
            "SELECT * FROM participants WHERE tmux_pane = ? AND status != ? "
            "ORDER BY created_at DESC LIMIT 1",
            (pane, str(Status.DEAD)),
        ).fetchone()
        return Participant.from_row(row) if row else None

    def list_participants(self, *, include_dead: bool = False) -> list[Participant]:
        sql = "SELECT * FROM participants"
        args: tuple = ()
        if not include_dead:
            sql += " WHERE status != ?"
            args = (str(Status.DEAD),)
        sql += " ORDER BY created_at ASC"
        return [Participant.from_row(r) for r in self.db.execute(sql, args)]

    def children_of(self, pid: str) -> list[Participant]:
        return [
            Participant.from_row(r)
            for r in self.db.execute(
                "SELECT * FROM participants WHERE parent_id = ? ORDER BY created_at",
                (pid,),
            )
        ]

    def set_status(self, pid: str, status: Status) -> None:
        self.db.execute(
            "UPDATE participants SET status = ?, last_activity = ? WHERE id = ?",
            (str(status), now(), pid),
        )

    def touch(self, pid: str) -> None:
        self.db.execute(
            "UPDATE participants SET last_activity = ? WHERE id = ?", (now(), pid)
        )

    # ---- jobs ----------------------------------------------------------

    def create_job(self, job) -> None:
        self.db.execute(
            "INSERT INTO jobs (handle, caller_id, target_id, kind, prompt, "
            "state, result, error_code, created_at, finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                job.handle, job.caller_id, job.target_id, job.kind,
                job.prompt, job.state, job.result, job.error_code,
                job.created_at, job.finished_at,
            ),
        )

    def get_job(self, handle: str) -> Job | None:
        row = self.db.execute(
            "SELECT * FROM jobs WHERE handle = ?", (handle,)
        ).fetchone()
        if row is None:
            return None
        return Job.from_row(row)

    def finish_job(
        self, handle: str, *, state: str, result: str | None = None,
        error_code: str | None = None, finished_at: float | None = None,
    ) -> None:
        self.db.execute(
            "UPDATE jobs SET state = ?, result = ?, error_code = ?, "
            "finished_at = ? WHERE handle = ?",
            (state, result, error_code, finished_at, handle),
        )

    def list_jobs_for_caller(self, caller_id: str) -> list[Job]:
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE caller_id = ? ORDER BY created_at DESC",
            (caller_id,),
        ).fetchall()
        return [Job.from_row(r) for r in rows]

    def running_jobs_for_target(self, target_id: str) -> list[Job]:
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE target_id = ? AND state = ? "
            "ORDER BY created_at DESC",
            (target_id, "running"),
        ).fetchall()
        return [Job.from_row(r) for r in rows]

    # ---- bus ----------------------------------------------------------

    def bus_append(
        self, kind: str, *, from_id: str | None = None,
        to_id: str | None = None, payload: dict | None = None,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO bus (ts, from_id, to_id, kind, payload) VALUES (?,?,?,?,?)",
            (now(), from_id, to_id, kind, json.dumps(payload) if payload else None),
        )
        return cur.lastrowid

    def bus_tail(self, limit: int = 100, *, after_id: int = 0) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM bus WHERE id > ? ORDER BY id DESC LIMIT ?",
            (after_id, limit),
        ).fetchall()
        out = []
        for r in reversed(rows):
            d = dict(r)
            d["payload"] = json.loads(d["payload"]) if d["payload"] else None
            out.append(d)
        return out
