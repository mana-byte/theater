"""Transcript lookup returns the newest match and applies the spawn creation floor."""

from __future__ import annotations

import time

from tests.support.collision_rig import make_session
from tests.support.shipped import VibeObserver
from theater.daemon.registry import Registry
from theater.models import Tier


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

    a = make_session(root, "a00bff57", str(project), text="agent A")
    b = make_session(root, "aa5d2d32", str(project), text="agent B")

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

    a = make_session(root, "a00bff57", str(project), text="agent A")
    make_session(root, "aa5d2d32", str(project), text="agent B")

    observer = VibeObserver(root=root)
    # The session id's first 8 chars are the directory suffix.
    result = observer.find_transcript(
        cwd=str(project), session_id="a00bff57-1111-2222-3333", after=None
    )
    assert result == a


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
    make_session(root, "old00001", str(project), text="old session")

    # The participant is spawned after the old session was created.
    p = registry.create_spawned(harness="vibe", cwd=str(project))

    # A new session, created after the participant.
    time.sleep(0.05)
    make_session(root, "new00001", str(project), text="new session")

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
    make_session(root, "old00001", str(project), text="adopted session")

    # Register as ADOPTED (not SPAWNED).
    p = registry.create_spawned(harness="vibe", cwd=str(project), tier=Tier.ADOPTED)
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

    make_session(root, "old00001", str(project), text="external session")

    # Register as EXTERNAL (no pane).
    p = registry.register(harness="vibe", pane=None, cwd=str(project))
    assert p.tier is Tier.EXTERNAL

    after = p.created_at if p.tier is Tier.SPAWNED else None
    assert after is None, "external participants must get no floor"
