"""Siblings in one cwd never share a transcript: ambiguity, exact claims and persisted locations."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.support.collision_rig import collision_observer as shared_collision_observer
from tests.support.collision_rig import collision_registry as shared_collision_registry
from tests.support.collision_rig import vibe_tree as shared_vibe_tree
from tests.support.shipped import VibeHarness, VibeObserver
from theater.daemon import methods as methods_mod
from theater.daemon.jobs import JobManager
from theater.daemon.observer import Observer, history_correlation_is_ambiguous
from theater.daemon.rpc import transcripts as transcripts_mod
from theater.harness.builtin.plugins.vibe.constants import ISOLATION_MARKER
from theater.harness.builtin.plugins.vibe.isolation import isolation_marker_text
from theater.harness.observation import ScreenKind, ScreenReading
from theater.harness.source import History
from theater.models import BadRequest, Status, TranscriptIdentityLost
from theater.provenance import TranscriptProvenance

vibe_tree = shared_vibe_tree  # the shared fixture
collision_registry = shared_collision_registry  # the shared fixture
collision_observer = shared_collision_observer  # the shared fixture


async def test_two_siblings_same_cwd_do_not_share_transcript(
    collision_registry, vibe_tree, collision_observer
):
    """The original collision: two siblings in one cwd, both session_id None.

    Neither participant may win a timing lottery. With no exact receipt, both
    cwd/time candidates are contested and fail closed.
    """
    from tests.support.waiting import until

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
    collision_registry, vibe_tree, monkeypatch, caplog
):
    first = collision_registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    collision_registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    jobs = JobManager(collision_registry.store)
    jobs.create(handle="ambiguous", caller_id="caller", target_id=first.id, kind="send")
    observer = Observer(collision_registry, harnesses={}, jobs=jobs, failure_grace=0.0)
    source = VibeObserver(root=vibe_tree["root"]).open_source(cwd=first.cwd)

    batch = await source.read()
    assert not observer._accept_attachment(first.id, source, batch)
    batch = await source.read()
    assert not observer._accept_attachment(first.id, source, batch)
    assert sum("refusing heuristic transcript" in record.message for record in caplog.records) == 1

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
    first = collision_registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    collision_registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    observer.start()
    try:
        from tests.support.waiting import until

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
