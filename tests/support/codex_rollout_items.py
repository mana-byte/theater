"""Helpers shared across a split test module."""

from __future__ import annotations

import json

from theater.harness.builtin.plugins.codex.observer import CodexObserver

THREAD = "01a0b0c0-0000-7000-8000-000000000001"


TS = "2026-09-10T14:00:00.000Z"


CWD = "/repo"


def _record(kind: str, payload: dict, ts: str = TS) -> dict:
    return {"timestamp": ts, "type": kind, "payload": payload}


def item_completed(turn_id: str, item: dict, *, started_ms: int, completed_ms: int) -> dict:
    return _record(
        "event_msg",
        {
            "type": "item_completed",
            "thread_id": THREAD,
            "turn_id": turn_id,
            "item": item,
            "started_at_ms": started_ms,
            "completed_at_ms": completed_ms,
        },
    )


def session_meta() -> dict:
    return _record("session_meta", {"id": THREAD, "cwd": CWD, "model": "gpt-5.6-sol"})


def _parsed(observer: CodexObserver, records: list[dict]):
    return [
        observer.parse_record(json.dumps(record), index) for index, record in enumerate(records)
    ]


def _events(parsed) -> list:
    return [event for record in parsed for event in record.events]


def _facts(parsed) -> list:
    return [fact for record in parsed for fact in record.trajectory]


def mcp_item(item_id: str, *, server: str = "theater", tool: str = "whoami") -> dict:
    return {
        "type": "McpToolCall",
        "id": item_id,
        "server": server,
        "tool": tool,
        "arguments": {"name": "audit"},
        "status": "completed",
        "result": {
            "content": [{"type": "text", "text": "whoami: ok"}],
            "isError": False,
        },
        "duration": {"secs": 1, "nanos": 500_000_000},
    }


def file_change_item(item_id: str, *, status: str = "completed") -> dict:
    return {
        "type": "FileChange",
        "id": item_id,
        "changes": {"theater/new.py": {"type": "add", "content": "x = 1\n"}},
        "status": status,
        "auto_approved": True,
        "stdout": "Applied patch.",
        "stderr": "",
    }
