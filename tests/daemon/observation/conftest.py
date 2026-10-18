"""Fixtures shared by the observation tests."""

from __future__ import annotations

import json

import pytest

from tests.support.live_observation import LiveOnlyRig
from tests.support.observation import (
    Rig,
)
from tests.support.shipped import VibeHarness
from theater.daemon.observer import (
    Observer,
)
from theater.harness.contracts.runtime import (
    SessionOpenMode,
)


@pytest.fixture
def vibe_tree(tmp_path):
    """A Vibe log root with one session whose cwd is a temporary project."""
    root = tmp_path / "logs" / "session"
    session = root / "session_20260101_000000_deadbeef"
    session.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    (session / "meta.json").write_text(
        json.dumps(
            {
                "session_id": "deadbeef-1111-2222-3333",
                "environment": {"working_directory": str(project)},
            }
        )
    )
    transcript = session / "messages.jsonl"
    transcript.write_text("")
    return {"root": root, "project": project, "transcript": transcript}


@pytest.fixture
async def observing(registry, vibe_tree):
    """An observer wound tight enough to finish inside a test."""
    harness = VibeHarness(root=vibe_tree["root"], isolated=True)
    observer = Observer(
        registry,
        {"vibe": harness},
        poll=0.01,
        search=0.01,
        sync=0.01,
    )
    observer.start()
    yield observer
    await observer.aclose()


@pytest.fixture
async def rig(store, registry, monkeypatch):
    r = Rig(store, registry, monkeypatch)
    registry.register(harness="fake", pane=None, cwd="/tmp", claimed_id="p1")
    await r.open()
    try:
        yield r
    finally:
        await r.aclose()


@pytest.fixture
async def live_only(store, registry):
    rig = LiveOnlyRig(store, registry)
    registry.register(harness="fake", pane=None, cwd="/tmp", claimed_id="p1")
    await rig.runtime.open_session(mode=SessionOpenMode.NEW)
    try:
        yield rig
    finally:
        await rig.observer.aclose()
        await rig.runtime.aclose()
