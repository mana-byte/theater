"""A SQLite file shaped like opencode 2.x's two-table projection, with step and part builders."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from theater.harness.builtin.plugins.opencode.source import OpenCodeSource

SCHEMA = """
CREATE TABLE session_v2 (
    id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT NOT NULL, time_created INTEGER NOT NULL
);
CREATE TABLE session_message (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL, seq INTEGER NOT NULL,
    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL
);
"""


class Recorder:
    def __init__(self, path: Path, sid: str, directory: str, created: int = 1000):
        self.path = path
        self.sid = sid
        self.clock = created
        self.seq = 0
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT INTO session_v2 VALUES (?, NULL, ?, ?)",
            (sid, str(Path(directory).resolve()), created),
        )
        self.conn.commit()

    def tick(self, ms: int = 10) -> int:
        self.clock += ms
        return self.clock

    def write(self, mid: str, kind: str, data: dict, *, at: int | None = None) -> None:
        """Insert, or rewrite in place keeping the row's `seq`, as the projector does."""
        at = self.tick() if at is None else at
        self.seq += 1
        self.conn.execute(
            "INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE "
            "SET data = excluded.data, time_updated = excluded.time_updated",
            (mid, self.sid, kind, self.seq, data["time"]["created"], at, json.dumps(data)),
        )
        self.conn.commit()

    def user(self, mid: str, text: str) -> None:
        self.write(mid, "user", {"time": {"created": self.tick()}, "text": text, "files": []})

    def step(self, mid: str, created: int, content: list, *, at: int | None = None, **fields):
        data = {
            "time": {"created": created, **fields.pop("times", {})},
            "agent": "build",
            "model": {"id": "claude", "providerID": "anthropic"},
            "content": content,
            **fields,
        }
        self.write(mid, "assistant", data, at=at)

    def idle(self, mid: str, outcome: str) -> None:
        self.write(mid, "idle", {"time": {"created": self.tick()}, "outcome": outcome})


def tool(call: str, status: str, path: str, output: str = "") -> dict:
    state: dict = {"status": status, "input": {"path": path}}
    if status == "completed":
        state["content"] = [{"type": "text", "text": output}]
    return {"type": "tool", "id": call, "name": "read", "state": state, "time": {"created": 1}}


def text(body: str) -> dict:
    return {"type": "text", "text": body}


USAGE = {"tokens": {"input": 3, "output": 5, "reasoning": 0, "cache": {"read": 0, "write": 0}}}


@pytest.fixture
def rec(tmp_path, workdir):
    r = Recorder(tmp_path / "opencode-v2.db", "ses_one", str(workdir))
    yield r
    r.conn.close()


def attached(rec, workdir) -> OpenCodeSource:
    src = OpenCodeSource(rec.path, cwd=str(workdir))
    batch = asyncio.run(src.read())
    assert batch.attached is not None
    src.commit_attachment()
    return src


def drain(src):
    """The conversation a read reports; usage-only step accounting is not part of it."""
    return [e for e in asyncio.run(src.read()).events if not e.usage_only]
