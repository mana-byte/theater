"""Restored locations are re-judged after a restart and persisted rows survive retention."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.support.collision_rig import collision_registry as shared_collision_registry
from tests.support.collision_rig import make_session, trust_pin
from tests.support.collision_rig import vibe_tree as shared_vibe_tree
from tests.support.shipped import VibeHarness, VibeObserver
from theater.daemon import methods as methods_mod
from theater.daemon.observer import Observer, history_correlation_is_ambiguous
from theater.daemon.rpc import transcripts as transcripts_mod
from theater.harness.source import History
from theater.models import BadRequest
from theater.provenance import TranscriptProvenance

vibe_tree = shared_vibe_tree  # the shared fixture
collision_registry = shared_collision_registry  # the shared fixture


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
        from tests.support.observation import bus_kinds
        from tests.support.waiting import until

        assert await until(lambda: "agent.transcript" in bus_kinds(collision_registry.store))
        with vibe_tree["transcript_b"].open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"role": "assistant", "content": str(provenance)}) + "\n")
        assert await until(lambda: "agent.assistant" in bus_kinds(collision_registry.store))
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
    make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))
    rotated = await source.refresh()
    evidence = await source.probe_identity_loss()

    assert rotated.attached is None
    assert evidence is not None
    assert evidence.location.endswith("zzzzzzzz/messages.jsonl")
    assert source.path == vibe_tree["transcript_b"]


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


def test_identity_loss_replays_across_restart_without_event_spam(collision_registry, vibe_tree):
    participant = collision_registry.register(
        harness="vibe", pane=None, cwd=str(vibe_tree["project"])
    )
    trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
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
