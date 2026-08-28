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
3. The observer's ``_accept_attachment`` refuses to bind a transcript already
   bound to a different live participant, and discards the staged candidate
   without changing the source's accepted cursor.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from shipped import ClaudeCodeHarness, VibeHarness, VibeObserver

from theater.daemon import methods as methods_mod
from theater.daemon import observer as observer_mod
from theater.daemon.jobs import JobManager
from theater.daemon.observer import (
    Observer,
    QuietClock,
    TurnAccumulator,
    history_correlation_is_ambiguous,
)
from theater.daemon.registry import Registry
from theater.daemon.rpc import transcripts as transcripts_mod
from theater.harness.builtin.plugins.vibe import ISOLATION_MARKER, isolation_marker_text
from theater.harness.observation import ScreenConfidence, ScreenKind, ScreenReading
from theater.harness.source import Attachment, Batch, History, Source
from theater.models import BadRequest, Status, Tier, TranscriptIdentityLost
from theater.provenance import TranscriptProvenance


class _StagedSource(Source):
    def __init__(self):
        self.committed = False
        self.discarded = False

    async def read(self) -> Batch:
        return Batch()

    def commit_attachment(self) -> None:
        self.committed = True

    def discard_attachment(self) -> None:
        self.discarded = True


def _trust_pin(registry: Registry, participant, transcript: Path, *, provenance: str = "operator"):
    participant.session_id = "a00bff57-1111-2222-3333"
    participant.session_correlation = provenance
    participant.transcript_location = str(transcript)
    participant.transcript_domain = str(transcript.parents[1].resolve())
    registry.store.upsert_participant(participant)
    return registry.get(participant.id)


async def _accept_bound_source(observer: Observer, harness: VibeHarness, participant):
    source = harness.observer.open_source(
        cwd=participant.cwd,
        session_id=participant.session_id,
        session_provenance=participant.session_correlation,
        known_location=participant.transcript_location,
    )
    batch = await source.read()
    assert batch.attached is not None
    assert observer._accept_attachment(participant.id, source, batch)
    observer._apply(participant.id, batch, QuietClock(), TurnAccumulator())
    return source


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


def _make_claude_transcript(root: Path, sid: str, cwd: str, *, text: str) -> Path:
    project = root / "-project"
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{sid}.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"type": "system", "cwd": cwd}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "id": f"message-{sid}",
                            "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": text}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


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

    Neither participant may win a timing lottery. With no exact receipt, both
    cwd/time candidates are contested and fail closed.
    """
    from tests.test_observer import until

    # Two participants in the same cwd, both with session_id None.
    # Registered as adopted (no pane, no birth-time floor) so the sessions
    # created in the fixture are not filtered out by the floor.
    collision_registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    collision_registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))

    # Wait until both watchers have opened their sources and attempted
    # discovery; no transcript event should be emitted from a contested guess.
    assert await until(
        lambda: len(collision_observer._sources) == 2,
        timeout=3.0,
    )
    await asyncio.sleep(0.05)

    assert not collision_observer._bound_transcripts
    assert not any(
        row["kind"] == "agent.transcript" for row in collision_registry.store.bus_tail(limit=500)
    )


async def test_initial_ambiguity_releases_the_await_as_an_explicit_crash(
    collision_registry, vibe_tree, monkeypatch
):
    first = collision_registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    collision_registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    jobs = JobManager(collision_registry.store)
    jobs.create(handle="ambiguous", caller_id="caller", target_id=first.id, kind="send")
    monkeypatch.setattr(observer_mod, "OBSERVATION_FAILURE_GRACE", 0.0)
    observer = Observer(collision_registry, harnesses={}, jobs=jobs)
    source = VibeObserver(root=vibe_tree["root"]).open_source(cwd=first.cwd)

    batch = await source.read()
    assert not observer._accept_attachment(first.id, source, batch)

    job = jobs.get("ambiguous")
    assert job.state == "crashed"
    assert job.error_code == "transcript_correlation_ambiguous"


async def test_rejected_initial_attachment_still_tracks_screen_status(
    collision_registry, vibe_tree
):
    """A failed-closed transcript must not freeze the display at IDLE.

    This is the Scapin regression: a second same-harness participant in one
    cwd has a real transcript candidate, but Theater cannot safely attribute
    it. The candidate stays rejected while the weaker pane channel remains
    useful for the cosmetic WORKING/IDLE status.
    """
    harness = VibeHarness(root=vibe_tree["root"])
    reading = {"kind": ScreenKind.WORKING}

    def screen_reading(_capture: str) -> ScreenReading:
        return ScreenReading(reading["kind"])

    harness.observer.screen_reading = screen_reading  # type: ignore[method-assign]
    observer = Observer(
        collision_registry,
        {"vibe": harness},
        poll=0.01,
        search=0.01,
        sync=0.01,
        awaiting=0.0,
    )

    async def capture_pane(_pane):
        return "rendered pane"

    observer._capture = capture_pane
    first = collision_registry.register(harness="vibe", pane="%1", cwd=str(vibe_tree["project"]))
    collision_registry.register(harness="vibe", pane="%2", cwd=str(vibe_tree["project"]))
    observer.start()
    try:
        from tests.test_observer import until

        assert await until(lambda: collision_registry.get(first.id).status is Status.WORKING)
        assert not observer._bound_transcripts

        reading["kind"] = ScreenKind.PROMPT
        assert await until(lambda: collision_registry.get(first.id).status is Status.IDLE)
        assert not observer._bound_transcripts
    finally:
        await observer.aclose()


async def test_exact_claim_revokes_an_earlier_heuristic_binding(collision_registry, vibe_tree):
    """A late process proof must repair, not merely diagnose, an early guess."""
    adapter = VibeObserver(root=vibe_tree["root"])
    observer = Observer(collision_registry, harnesses={})

    guessed = collision_registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    guessed_source = adapter.open_source(cwd=guessed.cwd)
    observer._sources[guessed.id] = guessed_source
    guessed_batch = await guessed_source.read()
    assert guessed_batch.attached.correlation == "heuristic"
    guessed_source.commit_attachment()
    observer._bound_transcripts[guessed_batch.attached.location] = guessed.id
    observer._binding_correlation[guessed_batch.attached.location] = "heuristic"
    observer._binding_sessions[guessed_batch.attached.location] = guessed_batch.attached.session_id
    guessed.session_id = guessed_batch.attached.session_id
    guessed.session_correlation = "heuristic"
    guessed.transcript_location = guessed_batch.attached.location
    collision_registry.store.upsert_participant(guessed)

    exact = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="aa5d2d32-1111-2222-3333",
    )
    exact.session_correlation = "exact"
    collision_registry.store.upsert_participant(exact)
    exact_source = adapter.open_source(
        cwd=exact.cwd,
        session_id=exact.session_id,
        session_provenance=TranscriptProvenance.EXACT,
    )
    observer._sources[exact.id] = exact_source
    exact_batch = await exact_source.read()
    assert exact_batch.attached.correlation == "exact"
    assert exact_batch.attached.location == guessed_batch.attached.location

    assert observer._accept_attachment(exact.id, exact_source, exact_batch)
    assert guessed_source.path is None
    repaired = collision_registry.get(guessed.id)
    assert repaired.session_id is None
    assert repaired.session_correlation is None
    assert repaired.transcript_location is None
    assert observer._bound_transcripts[exact_batch.attached.location] == exact.id


async def test_read_transcript_refuses_a_fulgenzio_style_heuristic_swap(
    collision_registry, vibe_tree, monkeypatch
):
    """A stored guessed id cannot make read_transcript serve its sibling."""
    harness = VibeHarness(root=vibe_tree["root"])
    monkeypatch.setitem(transcripts_mod.HARNESSES, "vibe", harness)
    domain = str(vibe_tree["root"].resolve())

    fulgenzio = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        # This is Senterello's id, persisted by the old buggy watcher.
        session_id="aa5d2d32-1111-2222-3333",
    )
    fulgenzio.session_correlation = "heuristic"
    fulgenzio.transcript_domain = domain
    collision_registry.store.upsert_participant(fulgenzio)
    senterello = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="aa5d2d32-1111-2222-3333",
    )
    senterello.session_correlation = "exact"
    senterello.transcript_domain = domain
    collision_registry.store.upsert_participant(senterello)

    daemon = SimpleNamespace(
        registry=collision_registry,
        observer=Observer(collision_registry, {"vibe": harness}),
    )
    with pytest.raises(BadRequest, match="transcript_correlation_untrusted"):
        await methods_mod.METHODS["read_transcript"](daemon, {"id": fulgenzio.id})


async def test_read_transcript_reopens_vibe_isolated_domain_through_factory(
    collision_registry, tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    domain = tmp_path / "isolated-vibe"
    session = domain / "session_20260817_120000_sessabc1"
    session.mkdir(parents=True)
    (session / "meta.json").write_text(
        json.dumps(
            {
                "session_id": "sessabc1-1111-2222-3333",
                "environment": {"working_directory": str(project)},
            }
        ),
        encoding="utf-8",
    )
    (session / "messages.jsonl").write_text(
        json.dumps({"role": "assistant", "content": "isolated"}) + "\n",
        encoding="utf-8",
    )
    participant = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(project),
        session_id="sessabc1-1111-2222-3333",
    )
    (domain / ISOLATION_MARKER).write_text(
        isolation_marker_text(participant_id=participant.id, transcript_domain=domain),
        encoding="utf-8",
    )
    participant.session_correlation = "exact"
    participant.transcript_domain = str(domain.resolve())
    collision_registry.store.upsert_participant(participant)
    harness = VibeHarness(root=tmp_path / "shared-vibe")
    monkeypatch.setitem(transcripts_mod.HARNESSES, "vibe", harness)
    daemon = SimpleNamespace(
        registry=collision_registry,
        observer=Observer(collision_registry, {"vibe": harness}),
    )

    result = await methods_mod.METHODS["read_transcript"](daemon, {"id": participant.id})

    assert result["path"] == str(session / "messages.jsonl")
    assert [event["text"] for event in result["events"]] == ["isolated"]


async def test_read_transcript_pinned_symlink_escape_fails_closed(
    collision_registry, tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    domain = tmp_path / "isolated-vibe"
    domain.mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps({"role": "assistant", "content": "outside"}) + "\n")
    link = domain / "escape.jsonl"
    link.symlink_to(outside)
    participant = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(project),
        session_id="sessabc1-1111-2222-3333",
    )
    (domain / ISOLATION_MARKER).write_text(
        isolation_marker_text(participant_id=participant.id, transcript_domain=domain),
        encoding="utf-8",
    )
    participant.session_correlation = "operator"
    participant.transcript_domain = str(domain.resolve())
    participant.transcript_location = str(link)
    collision_registry.store.upsert_participant(participant)
    harness = VibeHarness(root=tmp_path / "shared-vibe")
    monkeypatch.setitem(transcripts_mod.HARNESSES, "vibe", harness)
    daemon = SimpleNamespace(
        registry=collision_registry,
        observer=Observer(collision_registry, {"vibe": harness}),
    )

    with pytest.raises(TranscriptIdentityLost, match="no longer exists"):
        await methods_mod.METHODS["read_transcript"](daemon, {"id": participant.id})


async def test_restored_heuristic_location_is_rejudged_after_restart(collision_registry, vibe_tree):
    adapter = VibeObserver(root=vibe_tree["root"])
    observer = Observer(collision_registry, harnesses={})
    participant = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
    )
    participant.transcript_location = str(vibe_tree["transcript_b"])
    participant.session_correlation = "heuristic"
    collision_registry.store.upsert_participant(participant)
    source = adapter.open_source(
        cwd=participant.cwd,
        known_location=participant.transcript_location,
    )

    batch = await source.read()

    assert batch.attached is not None
    assert batch.attached.location == participant.transcript_location
    assert batch.attached.correlation == "heuristic"
    assert not observer._accept_attachment(participant.id, source, batch)
    assert source.path is None
    assert collision_registry.get(participant.id).transcript_location == str(
        vibe_tree["transcript_b"]
    )


@pytest.mark.parametrize(
    "provenance",
    [TranscriptProvenance.PROVEN, TranscriptProvenance.OPERATOR],
)
async def test_restored_trusted_location_attaches_after_restart(
    collision_registry, vibe_tree, provenance
):
    harness = VibeHarness(root=vibe_tree["root"])
    observer = Observer(
        collision_registry,
        {"vibe": harness},
        poll=0.01,
        search=0.01,
        sync=0.01,
    )
    participant = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="aa5d2d32-1111-2222-3333",
    )
    participant.session_correlation = str(provenance)
    participant.transcript_location = str(vibe_tree["transcript_b"])
    collision_registry.store.upsert_participant(participant)

    observer.start()
    try:
        from tests.test_observer import kinds, until

        assert await until(lambda: "agent.transcript" in kinds(collision_registry.store))
        with vibe_tree["transcript_b"].open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"role": "assistant", "content": str(provenance)}) + "\n")
        assert await until(lambda: "agent.assistant" in kinds(collision_registry.store))
    finally:
        await observer.aclose()


@pytest.mark.parametrize(
    "provenance",
    [TranscriptProvenance.PROVEN, TranscriptProvenance.OPERATOR],
)
async def test_read_transcript_trusts_restored_location_only(
    collision_registry, vibe_tree, monkeypatch, provenance
):
    harness = VibeHarness(root=vibe_tree["root"])
    monkeypatch.setitem(transcripts_mod.HARNESSES, "vibe", harness)
    participant = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="aa5d2d32-1111-2222-3333",
    )
    participant.session_correlation = str(provenance)
    participant.transcript_location = str(vibe_tree["transcript_b"])
    collision_registry.store.upsert_participant(participant)
    daemon = SimpleNamespace(
        registry=collision_registry,
        observer=Observer(collision_registry, {"vibe": harness}),
    )

    result = await methods_mod.METHODS["read_transcript"](daemon, {"id": participant.id})

    assert result["path"] == str(vibe_tree["transcript_b"])


@pytest.mark.parametrize(
    "provenance",
    [TranscriptProvenance.PROVEN, TranscriptProvenance.OPERATOR],
)
async def test_read_transcript_does_not_apply_restored_trust_without_location(
    collision_registry, vibe_tree, monkeypatch, provenance
):
    harness = VibeHarness(root=vibe_tree["root"])
    monkeypatch.setitem(transcripts_mod.HARNESSES, "vibe", harness)
    participant = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="aa5d2d32-1111-2222-3333",
    )
    participant.session_correlation = str(provenance)
    collision_registry.store.upsert_participant(participant)
    daemon = SimpleNamespace(
        registry=collision_registry,
        observer=Observer(collision_registry, {"vibe": harness}),
    )

    with pytest.raises(BadRequest, match="transcript_correlation_untrusted"):
        await methods_mod.METHODS["read_transcript"](daemon, {"id": participant.id})


@pytest.mark.parametrize(
    "provenance",
    [TranscriptProvenance.PROVEN, TranscriptProvenance.OPERATOR],
)
async def test_trusted_location_does_not_bless_another_path(
    collision_registry, vibe_tree, provenance
):
    source = VibeObserver(root=vibe_tree["root"]).open_source(
        cwd=str(vibe_tree["project"]),
        session_id="aa5d2d32-1111-2222-3333",
        session_provenance=provenance,
        known_location=str(vibe_tree["transcript_b"]),
    )

    other = vibe_tree["transcript_a"]

    assert source.correlation_for(other, "aa5d2d32-1111-2222-3333") == str(
        TranscriptProvenance.HEURISTIC
    )


@pytest.mark.parametrize(
    "provenance",
    [TranscriptProvenance.PROVEN, TranscriptProvenance.OPERATOR],
)
async def test_trusted_location_rotation_requires_fresh_proof_or_rebind(
    collision_registry, vibe_tree, provenance
):
    adapter = VibeObserver(root=vibe_tree["root"])
    observer = Observer(collision_registry, harnesses={})
    participant = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="aa5d2d32-1111-2222-3333",
    )
    participant.session_correlation = str(provenance)
    participant.transcript_location = str(vibe_tree["transcript_b"])
    collision_registry.store.upsert_participant(participant)
    source = adapter.open_source(
        cwd=participant.cwd,
        session_id=participant.session_id,
        session_provenance=provenance,
        known_location=participant.transcript_location,
    )

    initial = await source.read()
    assert observer._accept_attachment(participant.id, source, initial)
    _make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))
    rotated = await source.refresh()
    evidence = await source.probe_identity_loss()

    assert rotated.attached is None
    assert evidence is not None
    assert evidence.location.endswith("zzzzzzzz/messages.jsonl")
    assert source.path == vibe_tree["transcript_b"]


def test_trusted_dead_owner_blocks_stranger_but_allows_successor(collision_registry, vibe_tree):
    observer = Observer(collision_registry, harnesses={})
    dead = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="aa5d2d32-1111-2222-3333",
    )
    dead.session_correlation = "exact"
    dead.transcript_location = str(vibe_tree["transcript_b"])
    collision_registry.store.upsert_participant(dead)
    collision_registry.mark_dead(dead.id)

    stranger = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="other-session",
    )
    stranger_source = _StagedSource()
    stranger_batch = Batch(
        attached=Attachment(
            location=str(vibe_tree["transcript_b"]),
            session_id="other-session",
            correlation="exact",
        )
    )
    assert not observer._accept_attachment(stranger.id, stranger_source, stranger_batch)
    assert stranger_source.discarded is True

    successor = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="aa5d2d32-1111-2222-3333",
    )
    successor_source = _StagedSource()
    successor_batch = Batch(
        attached=Attachment(
            location=str(vibe_tree["transcript_b"]),
            session_id="aa5d2d32-1111-2222-3333",
            correlation="exact",
        )
    )
    assert observer._accept_attachment(successor.id, successor_source, successor_batch)
    assert successor_source.committed is True


def test_proven_attach_does_not_downgrade_existing_exact_session_id(collision_registry, vibe_tree):
    observer = Observer(collision_registry, harnesses={})
    participant = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="exact-session",
    )
    participant.session_correlation = "exact"
    collision_registry.store.upsert_participant(participant)

    observer._on_attach(
        participant.id,
        Attachment(
            location=str(vibe_tree["transcript_b"]),
            session_id="process-proven-session",
            correlation="proven",
        ),
    )

    after = collision_registry.get(participant.id)
    assert after.session_id == "exact-session"
    assert after.session_correlation == "exact"
    assert after.transcript_location == str(vibe_tree["transcript_b"])


async def test_untrusted_global_vibe_rotation_is_quarantined_even_with_distinct_domain(
    collision_registry, vibe_tree, tmp_path
):
    """A distinct domain avoids false sibling collisions, but does not prove ownership."""
    adapter = VibeObserver(root=vibe_tree["root"])
    observer = Observer(collision_registry, harnesses={})
    global_domain = str(vibe_tree["root"].resolve())

    incumbent = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="a00bff57-1111-2222-3333",
    )
    incumbent.transcript_domain = global_domain
    collision_registry.store.upsert_participant(incumbent)
    source = adapter.open_source(
        cwd=incumbent.cwd,
        session_id=incumbent.session_id,
        session_provenance=TranscriptProvenance.EXACT,
    )
    observer._sources[incumbent.id] = source
    initial = await source.read()
    assert observer._accept_attachment(incumbent.id, source, initial)

    isolated = collision_registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    isolated.transcript_domain = str((tmp_path / "private-vibe").resolve())
    collision_registry.store.upsert_participant(isolated)

    rotated = _make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))
    candidate = await source.refresh()
    evidence = await source.probe_identity_loss()
    assert candidate.attached is None
    assert evidence is not None
    assert evidence.location == str(rotated)
    assert source.path != rotated


def test_distinct_persisted_locations_survive_dead_row_retention(collision_registry, vibe_tree):
    domain = str(vibe_tree["root"].resolve())
    current = collision_registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    current.transcript_domain = domain
    current.transcript_location = str(vibe_tree["transcript_a"])
    collision_registry.store.upsert_participant(current)
    predecessor = collision_registry.register(
        harness="vibe", pane=None, cwd=str(vibe_tree["project"])
    )
    predecessor.transcript_domain = domain
    predecessor.transcript_location = str(vibe_tree["transcript_b"])
    collision_registry.store.upsert_participant(predecessor)
    collision_registry.mark_dead(predecessor.id)

    history = History(
        location=current.transcript_location,
        correlation="heuristic",
        collision_domain=domain,
        pinned=True,
    )
    assert not history_correlation_is_ambiguous(collision_registry, current.id, history)


async def test_read_transcript_reports_a_missing_pin_instead_of_ambiguity(
    collision_registry, vibe_tree, tmp_path, monkeypatch
):
    harness = VibeHarness(root=vibe_tree["root"])
    monkeypatch.setitem(transcripts_mod.HARNESSES, "vibe", harness)
    participant = collision_registry.register(
        harness="vibe", pane=None, cwd=str(vibe_tree["project"])
    )
    participant.session_id = "missing-session"
    participant.session_correlation = "operator"
    participant.transcript_location = str(tmp_path / "gone" / "messages.jsonl")
    participant.transcript_domain = str(vibe_tree["root"].resolve())
    collision_registry.store.upsert_participant(participant)
    observer = Observer(collision_registry, {"vibe": harness})
    jobs = JobManager(collision_registry.store)
    jobs.create(handle="still-running", caller_id="caller", target_id=participant.id, kind="send")
    daemon = SimpleNamespace(
        registry=collision_registry,
        observer=observer,
    )

    with pytest.raises(TranscriptIdentityLost, match="no longer exists"):
        await methods_mod.METHODS["read_transcript"](daemon, {"id": participant.id})
    assert not observer.transcript_identity_lost(participant.id)
    assert jobs.get("still-running").state == "running"
    assert not any(
        row["kind"] == "agent.observation_error"
        for row in collision_registry.store.bus_tail(limit=500)
    )


def test_duplicate_persisted_location_remains_ambiguous(collision_registry, vibe_tree):
    domain = str(vibe_tree["root"].resolve())
    location = str(vibe_tree["transcript_b"])
    first = collision_registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    first.transcript_domain = domain
    first.transcript_location = location
    collision_registry.store.upsert_participant(first)
    second = collision_registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    second.transcript_domain = domain
    second.transcript_location = location
    collision_registry.store.upsert_participant(second)

    history = History(
        location=location,
        correlation="heuristic",
        collision_domain=domain,
        pinned=True,
    )
    assert history_correlation_is_ambiguous(collision_registry, first.id, history)


def test_pre_location_epoch_null_is_an_explicit_upgrade_allowance(collision_registry, vibe_tree):
    epoch = float(collision_registry.store.get_meta("transcript_location_epoch"))
    domain = str(vibe_tree["root"].resolve())
    current = collision_registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    current.transcript_domain = domain
    current.transcript_location = str(vibe_tree["transcript_a"])
    collision_registry.store.upsert_participant(current)
    legacy = collision_registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    collision_registry.mark_dead(legacy.id)
    legacy = collision_registry.get(legacy.id)
    legacy.transcript_domain = None
    legacy.transcript_location = None
    legacy.last_activity = epoch - 1
    collision_registry.store.upsert_participant(legacy)

    history = History(
        location=current.transcript_location,
        correlation="heuristic",
        collision_domain=domain,
        pinned=True,
    )
    assert not history_correlation_is_ambiguous(collision_registry, current.id, history)

    live_unknown = collision_registry.register(
        harness="vibe", pane=None, cwd=str(vibe_tree["project"])
    )
    live_unknown.last_activity = epoch - 1
    collision_registry.store.upsert_participant(live_unknown)
    assert history_correlation_is_ambiguous(collision_registry, current.id, history)


async def test_rejected_rotation_keeps_events_and_awaits_session_local(
    collision_registry, vibe_tree
):
    """A sibling's newer transcript must never replace an accepted cursor.

    This is the complete regression for the two reported symptoms. Both Vibe
    participants know their exact sessions initially. A's cwd-only refresh
    then finds B's newer file and is refused. B's next turn must not flap A's
    status or appear on A's bus; A's own old file must still be readable, and
    its turn end must wake an await already blocked on A's job.
    """
    observer_adapter = VibeObserver(root=vibe_tree["root"])
    p_a = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="a00bff57-1111-2222-3333",
    )
    p_b = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="aa5d2d32-1111-2222-3333",
    )
    jobs = JobManager(collision_registry.store)
    observer = Observer(collision_registry, harnesses={}, jobs=jobs)
    source_a = observer_adapter.open_source(
        cwd=p_a.cwd,
        session_id=p_a.session_id,
        after=None,
        session_provenance=TranscriptProvenance.EXACT,
    )
    source_b = observer_adapter.open_source(
        cwd=p_b.cwd,
        session_id=p_b.session_id,
        after=None,
        session_provenance=TranscriptProvenance.EXACT,
    )
    turns_a = TurnAccumulator()

    batch_a = await source_a.read()
    batch_b = await source_b.read()
    assert observer._accept_attachment(p_a.id, source_a, batch_a)
    assert observer._accept_attachment(p_b.id, source_b, batch_b)
    observer._apply(p_a.id, batch_a, QuietClock(), turns_a)
    observer._apply(p_b.id, batch_b, QuietClock(), TurnAccumulator())
    assert len(observer._bound_transcripts) == 2

    # Cwd-only relocation finds B only through the non-committable loss probe.
    # It must be a no-op on A's accepted path.
    candidate = await source_a.refresh()
    evidence = await source_a.probe_identity_loss()
    assert candidate.attached is None
    assert evidence is not None
    assert evidence.location == str(vibe_tree["transcript_b"])
    assert source_a.path == vibe_tree["transcript_a"]
    assert not any(key[0] == p_a.id for key in observer._source_errors)

    # Rejection throttles only the relocate arm. The same discoverable foreign
    # candidate must not be re-read on every 250ms poll forever.
    relocate_clock = QuietClock()
    relocate_clock.quiet_since = time.monotonic() - observer.relocate - 1
    await observer._on_quiet(p_a.id, observer_adapter, source_a, relocate_clock, TurnAccumulator())
    assert relocate_clock.quiet_for(time.monotonic()) < 0.1

    before = collision_registry.store.bus_tail(limit=500)[-1]["id"]
    with vibe_tree["transcript_b"].open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"role": "user", "content": "foreign prompt"}) + "\n")
        fh.write(json.dumps({"role": "assistant", "content": "foreign answer"}) + "\n")

    # A remains on its own quiet transcript. Only B can publish B's append.
    assert not (await source_a.read()).events
    foreign = await source_b.read()
    observer._apply(p_b.id, foreign, QuietClock(), TurnAccumulator())
    rows = collision_registry.store.bus_tail(limit=500, after_id=before)
    agent_rows = [r for r in rows if r["kind"].startswith("agent.")]
    assert agent_rows
    assert {r["from_id"] for r in agent_rows} == {p_b.id}
    assert collision_registry.get(p_a.id).status.value == "idle"

    jobs.create(handle="await-a", caller_id="caller", target_id=p_a.id, kind="send")
    waiting = asyncio.create_task(jobs.await_jobs(["await-a"], max_wait=1.0))
    await asyncio.sleep(0)
    assert not waiting.done(), "the regression must exercise an already-blocked await"
    with vibe_tree["transcript_a"].open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"role": "assistant", "content": "A is done"}) + "\n")

    own = await source_a.read()
    observer._apply(p_a.id, own, QuietClock(), turns_a)
    result = await asyncio.wait_for(waiting, timeout=0.2)
    assert result[0].state == "done"
    assert result[0].result == "A is done"


async def test_identity_loss_growing_pin_stays_bound_and_attributed(collision_registry, vibe_tree):
    harness = VibeHarness(root=vibe_tree["root"])
    observer = Observer(collision_registry, {"vibe": harness})
    participant = collision_registry.register(
        harness="vibe", pane="%1", cwd=str(vibe_tree["project"])
    )
    participant = _trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    source = await _accept_bound_source(observer, harness, participant)

    with vibe_tree["transcript_a"].open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"role": "assistant", "content": "still me"}) + "\n")
    batch = await source.read()
    assert batch.progressed
    observer._apply(participant.id, batch, QuietClock(), TurnAccumulator())

    assert not observer.transcript_identity_lost(participant.id)
    rows = collision_registry.store.bus_tail(limit=500)
    assert any(
        row["kind"] == "agent.assistant"
        and row["from_id"] == participant.id
        and row["payload"]["text"] == "still me"
        for row in rows
    )
    assert not any(
        row["kind"] == "agent.observation_error"
        and row["payload"]["code"] == "transcript_identity_lost"
        for row in rows
    )


@pytest.mark.parametrize(
    ("screen_kind", "lost"),
    [(ScreenKind.WORKING, True), (ScreenKind.PROMPT, False)],
)
async def test_claude_readable_pin_uses_newer_candidate_only_as_loss_evidence(
    collision_registry, tmp_path, screen_kind, lost
):
    root = tmp_path / "claude" / "projects"
    project = tmp_path / "project"
    project.mkdir()
    old = _make_claude_transcript(root, "old-session", str(project), text="mine")
    os.utime(old, ns=(1_000_000_000, 1_000_000_000))
    harness = ClaudeCodeHarness(root=root)
    harness.observer.screen_reading = lambda _capture: ScreenReading(  # type: ignore[method-assign]
        screen_kind, ScreenConfidence.HIGH
    )
    observer = Observer(collision_registry, {"claude": harness}, relocate=0.0)

    async def capture_pane(_pane):
        return "real Claude screen"

    observer._capture = capture_pane
    participant = collision_registry.register(
        harness="claude", pane="%1", cwd=str(project), session_id="old-session"
    )
    participant.session_correlation = "operator"
    participant.transcript_location = str(old)
    collision_registry.store.upsert_participant(participant)
    source = observer._open_source(participant.id, harness.observer)
    assert source is not None
    initial = await source.read()
    assert observer._accept_attachment(participant.id, source, initial)

    newer = _make_claude_transcript(root, "new-session", str(project), text="not mine")
    os.utime(newer, ns=(2_000_000_000, 2_000_000_000))
    clock = QuietClock()
    clock.quiet_since = time.monotonic() - 10
    # Two consecutive relocate windows with the same evidence location are
    # required before quarantine. The first call alone does not quarantine.
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    if lost:
        assert not observer.transcript_identity_lost(participant.id)
        clock.quiet_since = time.monotonic() - 10
        await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())

    assert observer.transcript_identity_lost(participant.id) is lost
    assert source.path == old
    assert old.read_text(encoding="utf-8"), "the motivating rotation keeps the old file"
    assert str(newer) not in observer._bound_transcripts
    rows = collision_registry.store.bus_tail(limit=500)
    assert not any(
        row["kind"] == "agent.assistant"
        and row["from_id"] == participant.id
        and row["payload"]["text"] == "not mine"
        for row in rows
    )


async def test_identity_loss_inert_idle_pin_stays_bound(collision_registry, vibe_tree):
    harness = VibeHarness(root=vibe_tree["root"])
    harness.observer.screen_reading = lambda _capture: ScreenReading(  # type: ignore[method-assign]
        ScreenKind.PROMPT, ScreenConfidence.HIGH
    )
    observer = Observer(collision_registry, {"vibe": harness}, relocate=0.0, awaiting=999.0)

    async def capture_pane(_pane):
        return "prompt"

    observer._capture = capture_pane
    participant = collision_registry.register(
        harness="vibe", pane="%1", cwd=str(vibe_tree["project"])
    )
    participant = _trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    source = await _accept_bound_source(observer, harness, participant)
    _make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))
    clock = QuietClock()
    clock.quiet_since = time.monotonic() - 10

    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())

    assert source.path == vibe_tree["transcript_a"]
    assert not observer.transcript_identity_lost(participant.id)
    assert collision_registry.get(participant.id).status is Status.IDLE


async def test_identity_loss_inert_working_new_candidate_enters_quarantine_once(
    collision_registry, vibe_tree, monkeypatch
):
    harness = VibeHarness(root=vibe_tree["root"])
    harness.observer.screen_reading = lambda _capture: ScreenReading(  # type: ignore[method-assign]
        ScreenKind.WORKING, ScreenConfidence.HIGH
    )
    jobs = JobManager(collision_registry.store)
    monkeypatch.setattr(observer_mod, "OBSERVATION_FAILURE_GRACE", 0.0)
    observer = Observer(collision_registry, {"vibe": harness}, relocate=0.0, jobs=jobs)

    async def capture_pane(_pane):
        return "working"

    observer._capture = capture_pane
    participant = collision_registry.register(
        harness="vibe", pane="%1", cwd=str(vibe_tree["project"])
    )
    participant = _trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    source = await _accept_bound_source(observer, harness, participant)
    jobs.create(handle="lost-job", caller_id="caller", target_id=participant.id, kind="send")
    newer = _make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))
    clock = QuietClock()
    clock.quiet_since = time.monotonic() - 10

    # Two consecutive relocate windows with the same evidence are required.
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    assert not observer.transcript_identity_lost(participant.id)
    clock.quiet_since = time.monotonic() - 10
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    observer.mark_transcript_identity_lost(participant.id, "repeat should not spam")

    assert source.path == vibe_tree["transcript_a"]
    assert collision_registry.get(participant.id).transcript_location == str(
        vibe_tree["transcript_a"]
    )
    assert str(newer) not in observer._bound_transcripts
    assert collision_registry.get(participant.id).status is Status.WORKING
    job = jobs.get("lost-job")
    assert job.state == "crashed"
    assert job.error_code == "transcript_identity_lost"
    rows = [
        row
        for row in collision_registry.store.bus_tail(limit=500)
        if row["kind"] == "agent.observation_error"
        and row["to_id"] == participant.id
        and row["payload"]["code"] == "transcript_identity_lost"
    ]
    assert len(rows) == 1


def test_identity_loss_predicate_does_not_probe_or_transition(collision_registry, tmp_path):
    observer = Observer(collision_registry, harnesses={})
    participant = collision_registry.register(harness="vibe", pane="%1", cwd=str(tmp_path))
    participant.session_id = "missing-session"
    participant.session_correlation = "operator"
    participant.transcript_location = str(tmp_path / "gone" / "messages.jsonl")
    collision_registry.store.upsert_participant(participant)

    assert not observer.transcript_identity_lost(participant.id)
    assert not observer.transcript_identity_lost(participant.id)

    rows = [
        row
        for row in collision_registry.store.bus_tail(limit=500)
        if row["kind"] == "agent.observation_error"
        and row["to_id"] == participant.id
        and row["payload"]["code"] == "transcript_identity_lost"
    ]
    assert rows == []


async def test_identity_loss_rebind_rearms_and_is_idempotent(
    collision_registry, vibe_tree, monkeypatch
):
    harness = VibeHarness(root=vibe_tree["root"])
    monkeypatch.setitem(transcripts_mod.HARNESSES, "vibe", harness)
    observer = Observer(collision_registry, {"vibe": harness})
    participant = collision_registry.register(
        harness="vibe", pane="%1", cwd=str(vibe_tree["project"])
    )
    participant = _trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    observer.mark_transcript_identity_lost(participant.id, "rotation evidence")
    daemon = SimpleNamespace(
        registry=collision_registry, observer=observer, store=collision_registry.store
    )

    first = await methods_mod.METHODS["transcript.bind"](
        daemon,
        {
            "id": participant.id,
            "candidate": str(vibe_tree["transcript_b"]),
            "confirm_id": participant.id,
        },
    )
    second = await methods_mod.METHODS["transcript.bind"](
        daemon,
        {
            "id": participant.id,
            "candidate": str(vibe_tree["transcript_b"]),
            "confirm_id": participant.id,
        },
    )

    assert first["location"] == str(vibe_tree["transcript_b"].resolve())
    assert second["location"] == first["location"]
    assert not observer.transcript_identity_lost(participant.id)
    source = await _accept_bound_source(observer, harness, collision_registry.get(participant.id))
    assert source.path == vibe_tree["transcript_b"]


def test_identity_loss_replays_across_restart_without_event_spam(collision_registry, vibe_tree):
    participant = collision_registry.register(
        harness="vibe", pane="%1", cwd=str(vibe_tree["project"])
    )
    _trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    first = Observer(collision_registry, harnesses={})
    first.mark_transcript_identity_lost(participant.id, "rotation evidence")
    restarted = Observer(collision_registry, harnesses={})
    restarted._restore_transcript_identity_loss(participant.id)

    assert restarted.transcript_identity_lost(participant.id)
    rows = [
        row
        for row in collision_registry.store.bus_tail(limit=500)
        if row["kind"] == "agent.observation_error"
        and row["to_id"] == participant.id
        and row["payload"]["code"] == "transcript_identity_lost"
    ]
    assert len(rows) == 1


async def test_unique_heuristic_candidate_is_never_auto_adopted(collision_registry, vibe_tree):
    harness = VibeHarness(root=vibe_tree["root"])
    observer = Observer(collision_registry, {"vibe": harness})
    participant = collision_registry.register(
        harness="vibe", pane="%1", cwd=str(vibe_tree["project"])
    )
    source = harness.observer.open_source(cwd=participant.cwd)

    batch = await source.read()

    assert batch.attached is not None
    assert batch.attached.correlation == "heuristic"
    assert not observer._accept_attachment(participant.id, source, batch)
    assert source.path is None
    assert collision_registry.get(participant.id).transcript_location is None


async def test_accepted_rotation_commits_and_releases_the_old_binding(
    collision_registry, vibe_tree
):
    """A genuinely unowned rotation atomically replaces the old binding."""
    adapter = VibeObserver(root=vibe_tree["root"], isolated=True)
    p = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="a00bff57-1111-2222-3333",
    )
    p.session_correlation = "exact"
    collision_registry.store.upsert_participant(p)
    observer = Observer(collision_registry, harnesses={})
    source = adapter.open_source(
        cwd=p.cwd,
        session_id=p.session_id,
        after=None,
        session_provenance=TranscriptProvenance.EXACT,
    )
    initial = await source.read()
    assert observer._accept_attachment(p.id, source, initial)
    old = str(vibe_tree["transcript_a"])
    assert observer._bound_transcripts[old] == p.id

    newest = _make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))
    candidate = await source.refresh()
    assert candidate.attached is not None
    assert source.path == vibe_tree["transcript_a"]
    assert observer._accept_attachment(p.id, source, candidate)

    assert source.path == newest
    assert old not in observer._bound_transcripts
    assert observer._bound_transcripts[str(newest)] == p.id


class IncompleteAttachmentSource(Source):
    async def read(self) -> Batch:
        return Batch(attached=Attachment("somewhere", correlation="exact"))


async def test_attachment_source_without_handshake_fails_loudly(collision_registry):
    """A third-party source cannot silently reintroduce eager attachment."""
    p = collision_registry.register(harness="vibe", pane=None, cwd="/tmp")
    observer = Observer(collision_registry, harnesses={})
    source = IncompleteAttachmentSource()
    batch = await source.read()

    with pytest.raises(NotImplementedError, match="commit_attachment"):
        observer._accept_attachment(p.id, source, batch)


async def test_attachment_check_failure_discards_the_candidate(
    collision_registry, vibe_tree, monkeypatch
):
    """A transient store failure must not leave every later read wedged."""
    adapter = VibeObserver(root=vibe_tree["root"])
    p = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="a00bff57-1111-2222-3333",
    )
    p.session_correlation = "exact"
    collision_registry.store.upsert_participant(p)
    observer = Observer(collision_registry, harnesses={})
    source = adapter.open_source(
        cwd=p.cwd,
        session_id=p.session_id,
        after=None,
        session_provenance=TranscriptProvenance.EXACT,
    )
    batch = await source.read()
    assert batch.attached is not None
    observer._bound_transcripts[batch.attached.location] = "other"

    original = collision_registry.store.get_participant

    def fail_once(_pid):
        raise OSError("transient store failure")

    monkeypatch.setattr(collision_registry.store, "get_participant", fail_once)
    with pytest.raises(OSError, match="transient"):
        observer._accept_attachment(p.id, source, batch)
    monkeypatch.setattr(collision_registry.store, "get_participant", original)

    retry = await source.read()
    assert retry.attached is not None
    source.discard_attachment()


async def test_observer_releases_binding_on_watcher_end(
    collision_registry, vibe_tree, collision_observer
):
    """When a watcher ends, its transcript binding is released.

    This ensures a new participant starting later in the same cwd can bind
    the path the previous one held.
    """
    from tests.test_observer import until

    p_a = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="aa5d2d32-1111-2222-3333",
    )
    p_a.session_correlation = "exact"
    collision_registry.store.upsert_participant(p_a)

    assert await until(lambda: len(collision_observer._bound_transcripts) > 0, timeout=3.0)

    # Kill the participant — the watcher ends and releases the binding.
    collision_registry.mark_dead(p_a.id)

    assert await until(lambda: len(collision_observer._bound_transcripts) == 0, timeout=3.0)


# ---- Phase 1: identity-loss evidence safety and confirmation ---------------


async def test_identity_loss_evidence_bound_to_another_live_participant_is_rejected(
    collision_registry, vibe_tree
):
    """Loss evidence whose location is another live participant's transcript is rejected.

    Proves the full path: the source's ``probe_identity_loss`` actually finds
    the foreign candidate, the reducer's ownership guard rejects it, and
    quarantine is never entered — even after two relocate windows.
    """
    harness = VibeHarness(root=vibe_tree["root"])
    harness.observer.screen_reading = lambda _capture: ScreenReading(  # type: ignore[method-assign]
        ScreenKind.WORKING, ScreenConfidence.HIGH
    )
    observer = Observer(collision_registry, {"vibe": harness}, relocate=0.0)

    async def capture_pane(_pane):
        return "working"

    observer._capture = capture_pane

    # Sibling B owns transcript_b with an exact pin.
    sibling = collision_registry.register(
        harness="vibe",
        pane="%2",
        cwd=str(vibe_tree["project"]),
        session_id="aa5d2d32-1111-2222-3333",
    )
    sibling.session_correlation = "exact"
    sibling.transcript_location = str(vibe_tree["transcript_b"])
    collision_registry.store.upsert_participant(sibling)
    observer._bound_transcripts[str(vibe_tree["transcript_b"])] = sibling.id
    observer._binding_correlation[str(vibe_tree["transcript_b"])] = "exact"
    observer._binding_sessions[str(vibe_tree["transcript_b"])] = "aa5d2d32-1111-2222-3333"

    # Participant A has a trusted pin on transcript_a.
    participant = collision_registry.register(
        harness="vibe", pane="%1", cwd=str(vibe_tree["project"])
    )
    participant = _trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    source = await _accept_bound_source(observer, harness, participant)

    # The probe must actually find transcript_b as a newer candidate.
    evidence = await source.probe_identity_loss()
    assert evidence is not None, "probe must find the foreign candidate"
    assert evidence.location == str(vibe_tree["transcript_b"])
    assert evidence.session_id == "aa5d2d32-1111-2222-3333"

    # The reducer's ownership guard must reject it.
    assert observer._evidence_is_bound_to_another_live_participant(participant.id, evidence)

    # Even after two relocate windows, quarantine must not be entered.
    clock = QuietClock()
    clock.quiet_since = time.monotonic() - 10
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    clock.quiet_since = time.monotonic() - 10
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    assert not observer.transcript_identity_lost(participant.id)


async def test_identity_loss_evidence_session_id_matching_another_live_participant_is_rejected(
    collision_registry, vibe_tree
):
    """Loss evidence whose session_id matches another live participant is rejected."""
    observer = Observer(collision_registry, harnesses={})

    # Sibling has a different transcript_location but the same session_id.
    sibling = collision_registry.register(
        harness="vibe",
        pane="%2",
        cwd=str(vibe_tree["project"]),
        session_id="shared-session-id",
    )
    sibling.session_correlation = "exact"
    sibling.transcript_location = str(vibe_tree["transcript_b"])
    collision_registry.store.upsert_participant(sibling)

    participant = collision_registry.register(
        harness="vibe", pane="%1", cwd=str(vibe_tree["project"])
    )

    from theater.harness.source import IdentityLossEvidence

    evidence = IdentityLossEvidence(
        location="/some/other/path",
        session_id="shared-session-id",
    )
    assert observer._evidence_is_bound_to_another_live_participant(participant.id, evidence)


async def test_identity_loss_confirmation_requires_two_windows_with_same_location(
    collision_registry, vibe_tree
):
    """One relocate window is not enough; two with the same location are required."""
    harness = VibeHarness(root=vibe_tree["root"])
    harness.observer.screen_reading = lambda _capture: ScreenReading(  # type: ignore[method-assign]
        ScreenKind.WORKING, ScreenConfidence.HIGH
    )
    observer = Observer(collision_registry, {"vibe": harness}, relocate=0.0)

    async def capture_pane(_pane):
        return "working"

    observer._capture = capture_pane
    participant = collision_registry.register(
        harness="vibe", pane="%1", cwd=str(vibe_tree["project"])
    )
    participant = _trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    source = await _accept_bound_source(observer, harness, participant)
    _make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))

    clock = QuietClock()
    clock.quiet_since = time.monotonic() - 10

    # First relocate window: evidence found, but no quarantine yet.
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    assert not observer.transcript_identity_lost(participant.id)
    assert participant.id in observer._identity_loss_pending

    # Second relocate window: same evidence location, now quarantine.
    clock.quiet_since = time.monotonic() - 10
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    assert observer.transcript_identity_lost(participant.id)


async def test_identity_loss_confirmation_resets_on_semantic_progress(
    collision_registry, vibe_tree
):
    """Semantic progress on the pinned source resets the confirmation counter."""
    harness = VibeHarness(root=vibe_tree["root"])
    harness.observer.screen_reading = lambda _capture: ScreenReading(  # type: ignore[method-assign]
        ScreenKind.WORKING, ScreenConfidence.HIGH
    )
    observer = Observer(collision_registry, {"vibe": harness}, relocate=0.0)

    async def capture_pane(_pane):
        return "working"

    observer._capture = capture_pane
    participant = collision_registry.register(
        harness="vibe", pane="%1", cwd=str(vibe_tree["project"])
    )
    participant = _trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    source = await _accept_bound_source(observer, harness, participant)
    _make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))

    clock = QuietClock()
    clock.quiet_since = time.monotonic() - 10

    # First relocate window: evidence found, counter at 1.
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    assert participant.id in observer._identity_loss_pending
    assert observer._identity_loss_pending[participant.id][1] == 1

    # Actual source progress on the pinned source resets the counter.
    # An empty Batch() is a normal poll with no new data, not progress.
    progress_batch = Batch(progressed=True)
    observer._clear_source_error_on_progress(participant.id, progress_batch)
    assert participant.id not in observer._identity_loss_pending

    # Next relocate window starts fresh: counter at 1 again, not 2.
    clock.quiet_since = time.monotonic() - 10
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    assert not observer.transcript_identity_lost(participant.id)
    assert observer._identity_loss_pending[participant.id][1] == 1


async def test_identity_loss_confirmation_resets_when_location_changes(
    collision_registry, vibe_tree
):
    """A different evidence location resets the confirmation counter."""
    harness = VibeHarness(root=vibe_tree["root"])
    harness.observer.screen_reading = lambda _capture: ScreenReading(  # type: ignore[method-assign]
        ScreenKind.WORKING, ScreenConfidence.HIGH
    )
    observer = Observer(collision_registry, {"vibe": harness}, relocate=0.0)

    async def capture_pane(_pane):
        return "working"

    observer._capture = capture_pane
    participant = collision_registry.register(
        harness="vibe", pane="%1", cwd=str(vibe_tree["project"])
    )
    participant = _trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    source = await _accept_bound_source(observer, harness, participant)
    first_new = _make_session(vibe_tree["root"], "yyyyyyyy", str(vibe_tree["project"]))

    clock = QuietClock()
    clock.quiet_since = time.monotonic() - 10
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    assert observer._identity_loss_pending[participant.id] == (str(first_new), 1)

    # A different candidate location resets the counter to 1, not 2.
    second_new = _make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))
    clock.quiet_since = time.monotonic() - 10
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    assert not observer.transcript_identity_lost(participant.id)
    assert observer._identity_loss_pending[participant.id] == (str(second_new), 1)


# ---- B1/B2/B3 regression tests -------------------------------------------


async def test_identity_loss_grace_sweep_crashes_job_after_grace_in_quarantine(
    collision_registry, vibe_tree, monkeypatch
):
    """B1: a job that survives initial quarantine is crashed by the periodic sweep.

    The quarantine tick runs ``_sweep_identity_lost_grace`` on every iteration.
    A fresh job stays RUNNING initially, but after ``OBSERVATION_FAILURE_GRACE``
    elapses, the sweep deterministically crashes it.
    """
    harness = VibeHarness(root=vibe_tree["root"])
    harness.observer.screen_reading = lambda _capture: ScreenReading(  # type: ignore[method-assign]
        ScreenKind.WORKING, ScreenConfidence.HIGH
    )
    jobs = JobManager(collision_registry.store)
    monkeypatch.setattr(observer_mod, "OBSERVATION_FAILURE_GRACE", 0.0)
    observer = Observer(collision_registry, {"vibe": harness}, relocate=0.0, jobs=jobs)

    async def capture_pane(_pane):
        return "working"

    observer._capture = capture_pane
    participant = collision_registry.register(
        harness="vibe", pane="%1", cwd=str(vibe_tree["project"])
    )
    participant = _trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    source = await _accept_bound_source(observer, harness, participant)
    jobs.create(handle="sweep-job", caller_id="caller", target_id=participant.id, kind="send")
    _make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))

    # Enter quarantine via two relocate windows.
    clock = QuietClock()
    clock.quiet_since = time.monotonic() - 10
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    clock.quiet_since = time.monotonic() - 10
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    assert observer.transcript_identity_lost(participant.id)

    # With 0 grace, the sweep on the first quarantine tick crashes the job.
    observer._sweep_identity_lost_grace(participant.id)
    assert jobs.get("sweep-job").state == "crashed"
    assert jobs.get("sweep-job").error_code == "transcript_identity_lost"


async def test_identity_loss_grace_sweep_preserves_fresh_job_in_quarantine(
    collision_registry, vibe_tree, monkeypatch
):
    """B1: a fresh job stays RUNNING in quarantine when grace is positive."""
    harness = VibeHarness(root=vibe_tree["root"])
    harness.observer.screen_reading = lambda _capture: ScreenReading(  # type: ignore[method-assign]
        ScreenKind.WORKING, ScreenConfidence.HIGH
    )
    jobs = JobManager(collision_registry.store)
    monkeypatch.setattr(observer_mod, "OBSERVATION_FAILURE_GRACE", 30.0)
    observer = Observer(collision_registry, {"vibe": harness}, relocate=0.0, jobs=jobs)

    async def capture_pane(_pane):
        return "working"

    observer._capture = capture_pane
    participant = collision_registry.register(
        harness="vibe", pane="%1", cwd=str(vibe_tree["project"])
    )
    participant = _trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    source = await _accept_bound_source(observer, harness, participant)
    jobs.create(handle="fresh-job", caller_id="caller", target_id=participant.id, kind="send")
    _make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))

    clock = QuietClock()
    clock.quiet_since = time.monotonic() - 10
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    clock.quiet_since = time.monotonic() - 10
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    assert observer.transcript_identity_lost(participant.id)

    # Fresh job within grace: sweep must not crash it.
    observer._sweep_identity_lost_grace(participant.id)
    assert jobs.get("fresh-job").state == "running"


async def test_identity_loss_grace_sweep_uses_persisted_failed_at_on_restart(
    collision_registry, vibe_tree, monkeypatch
):
    """B1: restart replay uses the persisted bus timestamp, not now(), for failed_at."""
    jobs = JobManager(collision_registry.store)
    monkeypatch.setattr(observer_mod, "OBSERVATION_FAILURE_GRACE", 0.0)

    participant = collision_registry.register(
        harness="vibe", pane="%1", cwd=str(vibe_tree["project"])
    )
    participant = _trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    # Mark identity loss with the first observer.
    first = Observer(collision_registry, harnesses={}, jobs=jobs)
    first.mark_transcript_identity_lost(participant.id, "rotation evidence")
    assert first.transcript_identity_lost(participant.id)

    # A job created after the first observer's quarantine.
    jobs.create(handle="restart-job", caller_id="caller", target_id=participant.id, kind="send")

    # Restart: the new observer replays from the persisted bus timestamp.
    # With 0 grace, the sweep should immediately crash the job because
    # the persisted failed_at predates the job's creation.
    restarted = Observer(collision_registry, harnesses={}, jobs=jobs)
    restarted._restore_transcript_identity_loss(participant.id)
    assert restarted.transcript_identity_lost(participant.id)
    assert jobs.get("restart-job").state == "crashed"
    assert jobs.get("restart-job").error_code == "transcript_identity_lost"


async def test_identity_loss_confirmation_appear_gap_appear_resets(collision_registry, tmp_path):
    """B2: an evidence-free relocate window between two qualifying windows resets.

    appear -> gap (no evidence) -> appear must NOT quarantine on the third window,
    because the gap broke the consecutive chain.
    """
    import shutil

    # Use an isolated root so only the candidates we create exist.
    root = tmp_path / "isolated" / "session"
    root.mkdir(parents=True)
    project = tmp_path / "isolated_project"
    project.mkdir()

    # Trusted pin on transcript_a.
    pin = _make_session(root, "aaaaaaa", str(project), text="mine")
    os.utime(pin, ns=(1_000_000_000, 1_000_000_000))

    harness = VibeHarness(root=root)
    harness.observer.screen_reading = lambda _capture: ScreenReading(  # type: ignore[method-assign]
        ScreenKind.WORKING, ScreenConfidence.HIGH
    )
    observer = Observer(collision_registry, {"vibe": harness}, relocate=0.0)

    async def capture_pane(_pane):
        return "working"

    observer._capture = capture_pane
    participant = collision_registry.register(harness="vibe", pane="%1", cwd=str(project))
    participant = _trust_pin(collision_registry, participant, pin)
    source = await _accept_bound_source(observer, harness, participant)

    # Create a newer candidate.
    candidate = _make_session(root, "zzzzzzzz", str(project), text="not mine")
    os.utime(candidate, ns=(2_000_000_000, 2_000_000_000))

    # Window 1: evidence found, counter at 1.
    clock = QuietClock()
    clock.quiet_since = time.monotonic() - 10
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    assert observer._identity_loss_pending[participant.id] == (str(candidate), 1)

    # Gap: remove the candidate so the probe returns None.
    shutil.rmtree(candidate.parent)
    clock.quiet_since = time.monotonic() - 10
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    assert participant.id not in observer._identity_loss_pending

    # Window 3: candidate reappears, counter back to 1, not 2.
    candidate2 = _make_session(root, "zzzzzzzz", str(project), text="not mine")
    os.utime(candidate2, ns=(3_000_000_000, 3_000_000_000))
    clock.quiet_since = time.monotonic() - 10
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    assert not observer.transcript_identity_lost(participant.id)
    assert observer._identity_loss_pending[participant.id][1] == 1


async def test_identity_loss_confirmation_working_nonworking_gap_resets(
    collision_registry, vibe_tree
):
    """B2: a relocate window where the screen is not HIGH/WORKING resets."""
    harness = VibeHarness(root=vibe_tree["root"])
    reading = {"kind": ScreenKind.WORKING}
    harness.observer.screen_reading = lambda _capture: ScreenReading(  # type: ignore[method-assign]
        reading["kind"], ScreenConfidence.HIGH
    )
    observer = Observer(collision_registry, {"vibe": harness}, relocate=0.0)

    async def capture_pane(_pane):
        return "screen"

    observer._capture = capture_pane
    participant = collision_registry.register(
        harness="vibe", pane="%1", cwd=str(vibe_tree["project"])
    )
    participant = _trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    source = await _accept_bound_source(observer, harness, participant)
    _make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))

    # Window 1: HIGH/WORKING, evidence found, counter at 1.
    clock = QuietClock()
    clock.quiet_since = time.monotonic() - 10
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    assert observer._identity_loss_pending[participant.id][1] == 1

    # Gap: screen is PROMPT (not WORKING), so evidence is not admissible.
    reading["kind"] = ScreenKind.PROMPT
    clock.quiet_since = time.monotonic() - 10
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    assert participant.id not in observer._identity_loss_pending

    # Window 3: HIGH/WORKING again, counter back to 1, not 2.
    reading["kind"] = ScreenKind.WORKING
    clock.quiet_since = time.monotonic() - 10
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    assert not observer.transcript_identity_lost(participant.id)
    assert observer._identity_loss_pending[participant.id][1] == 1


async def test_identity_loss_quarantine_with_empty_polls_between_windows(
    collision_registry, vibe_tree
):
    """B3: empty polls between two qualifying relocate windows do not reset.

    Integration-style: simulates the real watch loop where normal polls return
    empty Batch() between relocate windows. The confirmation threshold must
    still be reached because empty polls have no actual source progress.
    """
    harness = VibeHarness(root=vibe_tree["root"])
    harness.observer.screen_reading = lambda _capture: ScreenReading(  # type: ignore[method-assign]
        ScreenKind.WORKING, ScreenConfidence.HIGH
    )
    observer = Observer(collision_registry, {"vibe": harness}, relocate=0.0)

    async def capture_pane(_pane):
        return "working"

    observer._capture = capture_pane
    participant = collision_registry.register(
        harness="vibe", pane="%1", cwd=str(vibe_tree["project"])
    )
    participant = _trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    source = await _accept_bound_source(observer, harness, participant)
    _make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))

    # Window 1: relocate fires, evidence found, counter at 1.
    clock = QuietClock()
    clock.quiet_since = time.monotonic() - 10
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    assert observer._identity_loss_pending[participant.id][1] == 1

    # Simulate normal empty polls between relocate windows. The watch loop
    # calls _clear_source_error_on_progress on each accepted batch. An empty
    # Batch() has error_code None but no progress/events/attachment.
    empty_batch = Batch()
    observer._clear_source_error_on_progress(participant.id, empty_batch)
    # Pending must survive because empty polls are not actual progress.
    assert participant.id in observer._identity_loss_pending
    assert observer._identity_loss_pending[participant.id][1] == 1

    # Window 2: same evidence location, quarantine entered.
    clock.quiet_since = time.monotonic() - 10
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    assert observer.transcript_identity_lost(participant.id)


async def test_identity_loss_actual_progress_resets_confirmation(collision_registry, vibe_tree):
    """B3: actual source progress (events on the pinned transcript) resets."""
    from theater.harness.base import Event, EventKind

    harness = VibeHarness(root=vibe_tree["root"])
    harness.observer.screen_reading = lambda _capture: ScreenReading(  # type: ignore[method-assign]
        ScreenKind.WORKING, ScreenConfidence.HIGH
    )
    observer = Observer(collision_registry, {"vibe": harness}, relocate=0.0)

    async def capture_pane(_pane):
        return "working"

    observer._capture = capture_pane
    participant = collision_registry.register(
        harness="vibe", pane="%1", cwd=str(vibe_tree["project"])
    )
    participant = _trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    source = await _accept_bound_source(observer, harness, participant)
    _make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))

    # Window 1: evidence found, counter at 1.
    clock = QuietClock()
    clock.quiet_since = time.monotonic() - 10
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    assert observer._identity_loss_pending[participant.id][1] == 1

    # Actual progress: a batch with events from the pinned source.
    progress_batch = Batch(events=[Event(kind=EventKind.ASSISTANT, text="alive")])
    observer._clear_source_error_on_progress(participant.id, progress_batch)
    assert participant.id not in observer._identity_loss_pending

    # Next window starts fresh: counter at 1, not 2.
    clock.quiet_since = time.monotonic() - 10
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    assert not observer.transcript_identity_lost(participant.id)
    assert observer._identity_loss_pending[participant.id][1] == 1


def test_identity_loss_evidence_cross_harness_location_is_rejected(collision_registry, vibe_tree):
    """Ownership guard rejects evidence regardless of the other participant's adapter.

    A live owner of the exact location disqualifies evidence even when the
    other participant runs under a different harness. The same-harness filter
    was removed because a transcript location is a physical file, and a
    different adapter does not make it safe to quarantine on.
    """
    observer = Observer(collision_registry, harnesses={})

    # Sibling under a *different* harness owns the location.
    sibling = collision_registry.register(
        harness="codex",
        pane="%2",
        cwd=str(vibe_tree["project"]),
    )
    sibling.transcript_location = str(vibe_tree["transcript_b"])
    collision_registry.store.upsert_participant(sibling)

    participant = collision_registry.register(
        harness="vibe", pane="%1", cwd=str(vibe_tree["project"])
    )

    from theater.harness.source import IdentityLossEvidence

    evidence = IdentityLossEvidence(location=str(vibe_tree["transcript_b"]))
    assert observer._evidence_is_bound_to_another_live_participant(participant.id, evidence)
