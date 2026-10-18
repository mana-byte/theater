"""Vibe-shaped transcript records and an append helper for observer tests."""

from __future__ import annotations

import json
from pathlib import Path

USER = {"role": "user", "content": "do the thing"}


WORKING = {
    "role": "assistant",
    "content": "on it",
    "tool_calls": [{"id": "c1", "function": {"name": "bash", "arguments": "{}"}}],
}


RESULT = {"role": "tool", "content": "output", "name": "bash", "tool_call_id": "c1"}


DONE = {"role": "assistant", "content": "finished"}


def append(path: Path, *records: dict) -> None:
    with path.open("a") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
        fh.flush()
