"""A vanished or inert pin quarantines the participant without spurious transitions."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.collision_rig import (
    accept_bound_source,
    make_session,
    trust_pin,
    write_claude_transcript,
)
from tests.support.collision_rig import collision_registry as shared_collision_registry
from tests.support.collision_rig import vibe_tree as shared_vibe_tree
from tests.support.shipped import ClaudeCodeHarness, VibeHarness
from theater.daemon import methods as methods_mod
from theater.daemon.jobs import JobManager
from theater.daemon.observer import Observer, QuietClock, TurnAccumulator
from theater.daemon.rpc import transcripts as transcripts_mod
from theater.harness.observation import ScreenConfidence, ScreenKind, ScreenReading
from theater.models import Status

vibe_tree = shared_vibe_tree  # the shared fixture
collision_registry = shared_collision_registry  # the shared fixture


def _make_claude_transcript(root: Path, sid: str, cwd: str, *, text: str) -> Path:
    return write_claude_transcript(root / "-project" / f"{sid}.jsonl", sid=sid, cwd=cwd, text=text)


async def test_identity_loss_growing_pin_stays_bound_and_attributed(collision_registry, vibe_tree):
    harness = VibeHarness(root=vibe_tree["root"])
    observer = Observer(collision_registry, {"vibe": harness})
    participant = collision_registry.register(
        harness="vibe", pane=None, cwd=str(vibe_tree["project"])
    )
    participant = trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    source = await accept_bound_source(observer, harness, participant)

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
        harness="claude", pane=None, cwd=str(project), session_id="old-session"
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
        harness="vibe", pane=None, cwd=str(vibe_tree["project"])
    )
    participant = trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    source = await accept_bound_source(observer, harness, participant)
    make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))
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
    observer = Observer(
        collision_registry, {"vibe": harness}, relocate=0.0, jobs=jobs, failure_grace=0.0
    )

    async def capture_pane(_pane):
        return "working"

    observer._capture = capture_pane
    participant = collision_registry.register(
        harness="vibe", pane=None, cwd=str(vibe_tree["project"])
    )
    participant = trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    source = await accept_bound_source(observer, harness, participant)
    jobs.create(handle="lost-job", caller_id="caller", target_id=participant.id, kind="send")
    newer = make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))
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
    participant = collision_registry.register(harness="vibe", pane=None, cwd=str(tmp_path))
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
        harness="vibe", pane=None, cwd=str(vibe_tree["project"])
    )
    participant = trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    observer.mark_transcript_identity_lost(participant.id, "rotation evidence")
    daemon = SimpleNamespace(
        registry=collision_registry, observer=observer, store=collision_registry.store
    )
    from tests.support.presence import FakePresence

    daemon.presence = FakePresence()

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
    source = await accept_bound_source(observer, harness, collision_registry.get(participant.id))
    assert source.path == vibe_tree["transcript_b"]
