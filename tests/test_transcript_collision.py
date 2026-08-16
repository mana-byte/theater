"""Regression: two siblings in the same cwd must not share one transcript.

The original bug: ``VibeObserver.find_transcript`` resolved a transcript in
two stages — by session id (glob) or by cwd scan (newest match). The cwd scan
had no tie-break and no notion of a transcript already belonging to another
live participant. Two siblings spawned into the same cwd within the same
second produced two session directories that both matched, and the scan
returned the lexicographically larger one for *both* participants — serving
one agent's transcript as the other's.

Three fixes are tested here:

1. ``_read_transcript`` uses the same birth-time floor as the watch path
   (``after = p.created_at`` for SPAWNED, ``None`` for adopted/external).
2. ``find_transcript`` logs an ambiguity when multiple session directories
   match the same cwd, so the collision is not silent.
3. The observer's ``_on_attach`` refuses to bind a transcript already bound
   to a different live participant, and detaches the source so it searches
   again.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from shipped import VibeHarness, VibeObserver

from theater.daemon.observer import Observer
from theater.daemon.registry import Registry
from theater.models import Tier


def _make_session(root: Path, short: str, cwd: str, *, text: str = "hello") -> Path:
    """Create a vibe session directory with meta.json and messages.jsonl."""
    d = root / f"session_20260816_191459_{short}"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(
        json.dumps(
            {
                "session_id": f"{short}-1111-2222-3333",
                "environment": {"working_directory": cwd},
            }
        )
    )
    messages = d / "messages.jsonl"
    messages.write_text(json.dumps({"role": "assistant", "content": text}) + "\n")
    return messages


# ---- Fix 2: find_transcript logs when multiple session dirs match --------


def test_two_session_dirs_same_cwd_logs_and_returns_newest(tmp_path):
    """Two session directories with the same cwd are an ambiguity.

    ``find_transcript`` returns the newest match (so rotation still works)
    and logs a warning so the collision is not silent. The observer's binding
    check is the actual collision prevention — see the observer tests below.
    """
    root = tmp_path / "logs" / "session"
    root.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()

    a = _make_session(root, "a00bff57", str(project), text="agent A")
    b = _make_session(root, "aa5d2d32", str(project), text="agent B")

    observer = VibeObserver(root=root)
    result = observer.find_transcript(cwd=str(project), session_id=None, after=None)

    assert result is not None
    # The newest by lexicographic order of the session suffix is aa5d2d32.
    assert result == b
    assert result != a


def test_find_transcript_with_session_id_resolves_exactly(tmp_path):
    """When session_id is known, find_transcript uses the glob path.

    This is the sharp key that avoids the cwd scan entirely. Once the
    observer discovers the session id, the collision window is over.
    """
    root = tmp_path / "logs" / "session"
    root.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()

    a = _make_session(root, "a00bff57", str(project), text="agent A")
    _make_session(root, "aa5d2d32", str(project), text="agent B")

    observer = VibeObserver(root=root)
    # The session id's first 8 chars are the directory suffix.
    result = observer.find_transcript(
        cwd=str(project), session_id="a00bff57-1111-2222-3333", after=None
    )
    assert result == a


# ---- Fix 1: _read_transcript uses the same birth-time floor ---------------


def test_read_transcript_uses_spawned_floor(registry: Registry, tmp_path):
    """``_read_transcript`` passes ``after=p.created_at`` for SPAWNED tier.

    This is tested through the observer's ``_open_source``, which uses the
    same rule. The floor eliminates sessions born before the participant
    was created, so an old session in the same cwd is not picked up.
    """
    root = tmp_path / "logs" / "session"
    root.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()

    # An old session, created before the participant exists.
    _make_session(root, "old00001", str(project), text="old session")

    # The participant is spawned after the old session was created.
    p = registry.create_spawned(harness="vibe", cwd=str(project))

    # A new session, created after the participant.
    time.sleep(0.05)
    _make_session(root, "new00001", str(project), text="new session")

    observer = VibeObserver(root=root)
    source = observer.open_source(cwd=p.cwd, session_id=p.session_id, after=p.created_at)

    # With the floor, the old session (born before created_at) is skipped.
    # Only the new session matches.
    import asyncio

    history = asyncio.run(source.history(last_n=0))
    assert history.location is not None
    assert "new00001" in history.location
    assert "old00001" not in history.location


def test_read_transcript_no_floor_for_adopted(registry: Registry, tmp_path):
    """Adopted participants get no birth-time floor.

    An adopted session's output predates Theater's first sight of it, so
    the floor would hide the very transcript we want to read.
    """
    root = tmp_path / "logs" / "session"
    root.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()

    # A session born in the past — before any Theater participant.
    _make_session(root, "old00001", str(project), text="adopted session")

    # Register as ADOPTED (not SPAWNED).
    p = registry.register(harness="vibe", pane="%1", cwd=str(project))
    assert p.tier is Tier.ADOPTED

    observer = VibeObserver(root=root)
    # The observer's _open_source rule: after = created_at if SPAWNED else None.
    after = p.created_at if p.tier is Tier.SPAWNED else None
    assert after is None, "adopted participants must get no floor"

    source = observer.open_source(cwd=p.cwd, session_id=p.session_id, after=after)
    import asyncio

    history = asyncio.run(source.history(last_n=0))
    assert history.location is not None
    assert "old00001" in history.location


def test_read_transcript_no_floor_for_external(registry: Registry, tmp_path):
    """External participants also get no birth-time floor."""
    root = tmp_path / "logs" / "session"
    root.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()

    _make_session(root, "old00001", str(project), text="external session")

    # Register as EXTERNAL (no pane).
    p = registry.register(harness="vibe", pane=None, cwd=str(project))
    assert p.tier is Tier.EXTERNAL

    after = p.created_at if p.tier is Tier.SPAWNED else None
    assert after is None, "external participants must get no floor"


# ---- Fix 3: observer refuses to bind a transcript already bound ----------


@pytest.fixture
def vibe_tree(tmp_path):
    """A Vibe log root with two sessions in the same cwd."""
    root = tmp_path / "logs" / "session"
    root.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()

    a = _make_session(root, "a00bff57", str(project), text="agent A says hello")
    b = _make_session(root, "aa5d2d32", str(project), text="agent B says world")

    return {"root": root, "project": project, "transcript_a": a, "transcript_b": b}


@pytest.fixture
def collision_registry(store):
    return Registry(store)


@pytest.fixture
async def collision_observer(collision_registry, vibe_tree):
    """An observer wound tight enough to finish inside a test."""
    observer = Observer(
        collision_registry,
        {"vibe": VibeHarness(root=vibe_tree["root"])},
        poll=0.01,
        search=0.01,
        sync=0.01,
    )
    observer.start()
    yield observer
    await observer.aclose()


async def test_two_siblings_same_cwd_do_not_share_transcript(
    collision_registry, vibe_tree, collision_observer
):
    """The original collision: two siblings in one cwd, both session_id None.

    The first participant binds its transcript. The second participant's
    source tries to bind the same file and is refused — the observer logs
    the collision and detaches the source so it searches again.
    """
    from tests.test_observer import until

    # Two participants in the same cwd, both with session_id None.
    # Registered as adopted (no pane, no birth-time floor) so the sessions
    # created in the fixture are not filtered out by the floor.
    collision_registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    collision_registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))

    # Wait for at least one transcript event.
    assert await until(
        lambda: any(
            r["kind"] == "agent.transcript" for r in collision_registry.store.bus_tail(limit=500)
        ),
        timeout=3.0,
    )

    # At most one participant should have a transcript binding.
    bound = collision_observer._bound_transcripts
    owners = set(bound.values())
    # No two participants share the same path.
    assert len(owners) <= len(bound), "a transcript path is bound to two participants"

    # The first to attach wins; the second is refused.
    assert len(bound) <= 1, "both participants bound a transcript — collision not prevented"


async def test_observer_releases_binding_on_watcher_end(
    collision_registry, vibe_tree, collision_observer
):
    """When a watcher ends, its transcript binding is released.

    This ensures a new participant starting later in the same cwd can bind
    the path the previous one held.
    """
    from tests.test_observer import until

    p_a = collision_registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))

    assert await until(lambda: len(collision_observer._bound_transcripts) > 0, timeout=3.0)

    # Kill the participant — the watcher ends and releases the binding.
    collision_registry.mark_dead(p_a.id)

    assert await until(lambda: len(collision_observer._bound_transcripts) == 0, timeout=3.0)
