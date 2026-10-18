"""A transcript file, source and attach helper for the transcript source tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.shipped import ClaudeCodeObserver
from theater.harness.source import TranscriptSource


def record(text: str, *, end: bool = True) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-06-24T18:26:15.348Z",
            "message": {
                "id": f"m-{text}",
                "stop_reason": "end_turn" if end else "tool_use",
                "content": [{"type": "text", "text": text}],
            },
        }
    )


@pytest.fixture
def root(tmp_path) -> Path:
    d = tmp_path / "projects"
    (d / "-work").mkdir(parents=True)
    return d


@pytest.fixture
def workdir(workdir) -> str:
    return str(workdir)


def transcript(root: Path, name: str, cwd: str, *lines: str) -> Path:
    path = root / "-work" / f"{name}.jsonl"
    head = json.dumps({"type": "system", "cwd": cwd})
    path.write_text("\n".join([head, *lines]) + "\n", encoding="utf-8")
    return path


def source(root: Path, workdir: str, **kw):
    # These generic source tests exercise the relocation protocol explicitly.
    # Shipped observers opt into cwd relocation only where their format needs
    # it (Vibe); using Claude's locator here keeps the fixture compact.
    return TranscriptSource(ClaudeCodeObserver(root=root), cwd=workdir, allow_refresh=True, **kw)


async def attach(s):
    """Stage and accept the initial attachment, as the observer does."""
    batch = await s.read()
    assert batch.attached is not None
    s.commit_attachment()
    return batch
