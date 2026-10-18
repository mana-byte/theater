"""A SQLite file shaped like opencode 1.x's database, written the way the server writes it."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

SCHEMA = """
CREATE TABLE session (
    id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT,
    time_created INTEGER, time_updated INTEGER
);
CREATE TABLE event (
    id INTEGER PRIMARY KEY AUTOINCREMENT, aggregate_id TEXT, seq INTEGER,
    type TEXT, data TEXT
);
CREATE TABLE message (
    id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
    time_updated INTEGER, data TEXT
);
CREATE TABLE part (
    id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
    time_created INTEGER, time_updated INTEGER, data TEXT
);
"""


class Recorder:
    """Writes a session the way opencode does: events plus current state.

    Both are kept in step because the adapter reads both — `read()` follows the
    event log, `history()` reads the message and part tables — and a fixture
    that let them disagree would let a bug in either go unnoticed.
    """

    def __init__(self, path: Path, sid: str, directory: str, created: int = 1000):
        self.path = path
        self.sid = sid
        self.seq = -1
        self.clock = created
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT INTO session (id, parent_id, directory, time_created) VALUES (?, NULL, ?, ?)",
            (sid, str(Path(directory).resolve()), created),
        )
        self.conn.commit()

    # ---- plumbing -------------------------------------------------------

    def tick(self, ms: int = 10) -> int:
        self.clock += ms
        return self.clock

    def emit(self, kind: str, data: dict) -> None:
        self.seq += 1
        self.conn.execute(
            "INSERT INTO event (aggregate_id, seq, type, data) VALUES (?, ?, ?, ?)",
            (self.sid, self.seq, kind, json.dumps(data)),
        )
        self.conn.commit()

    def _store_message(self, mid: str, info: dict) -> None:
        self.conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data",
            (mid, self.sid, info["time"]["created"], json.dumps(info)),
        )
        self.conn.commit()

    def _store_part(self, part: dict, when: int) -> None:
        self.conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, data) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data",
            (part["id"], part["messageID"], self.sid, when, json.dumps(part)),
        )
        self.conn.commit()

    def _part(self, part: dict) -> None:
        when = self.tick()
        self.emit("message.part.updated.1", {"part": part, "time": when})
        self._store_part(part, when)

    # ---- the shapes a session is made of --------------------------------

    def message(self, mid: str, role: str) -> dict:
        info = {"id": mid, "role": role, "time": {"created": self.tick()}}
        self.emit("message.updated.1", {"info": info})
        self._store_message(mid, info)
        return info

    def finish(self, info: dict, reason: str) -> None:
        """Both firings, in order, as opencode writes them."""
        info = dict(info, finish=reason)
        self.emit("message.updated.1", {"info": info})
        info = dict(info, time=dict(info["time"], completed=self.tick()))
        self.emit("message.updated.1", {"info": info})
        self._store_message(info["id"], info)

    def text(self, mid: str, pid: str, body: str) -> None:
        """Empty first, then replaced whole — never appended to."""
        self._part({"id": pid, "messageID": mid, "type": "text", "text": ""})
        self._part({"id": pid, "messageID": mid, "type": "text", "text": body})

    def user_text(self, mid: str, pid: str, body: str) -> None:
        """A user part arrives complete and only once."""
        self._part({"id": pid, "messageID": mid, "type": "text", "text": body})

    def tool(self, mid: str, pid: str, call: str, name: str, output: str) -> None:
        base = {
            "id": pid,
            "messageID": mid,
            "type": "tool",
            "callID": call,
            "tool": name,
        }
        self._part({**base, "state": {"status": "pending", "input": {}}})
        self._part({**base, "state": {"status": "running", "input": {"x": 1}}})
        self._part({**base, "state": {"status": "completed", "output": output}})

    def step_finish(self, mid: str, pid: str, reason: str) -> None:
        self._part({"id": pid, "messageID": mid, "type": "step-finish", "reason": reason})


@pytest.fixture
def rec(tmp_path, workdir):
    r = Recorder(tmp_path / "opencode-stable.db", "ses_one", str(workdir))
    yield r
    r.conn.close()
