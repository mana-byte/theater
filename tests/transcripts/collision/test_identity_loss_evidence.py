"""Identity-loss evidence bound to another live participant or harness is rejected."""

from __future__ import annotations

import time
from pathlib import Path

from tests.support.collision_rig import accept_bound_source, trust_pin, write_claude_transcript
from tests.support.collision_rig import collision_registry as shared_collision_registry
from tests.support.collision_rig import vibe_tree as shared_vibe_tree
from tests.support.observation import run_quiet_step
from tests.support.shipped import ClaudeCodeHarness, VibeHarness
from theater.daemon.observer import Observer, QuietClock, TurnAccumulator
from theater.harness.observation import ScreenConfidence, ScreenKind, ScreenReading
from theater.transcript_identity import TRANSCRIPT_IDENTITY_LOST_CODE

vibe_tree = shared_vibe_tree  # the shared fixture
collision_registry = shared_collision_registry  # the shared fixture


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
        pane=None,
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
        harness="vibe", pane=None, cwd=str(vibe_tree["project"])
    )
    participant = trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    source = await accept_bound_source(observer, harness, participant)

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
    await run_quiet_step(
        observer, participant.id, harness.observer, source, clock, TurnAccumulator()
    )
    clock.quiet_since = time.monotonic() - 10
    await run_quiet_step(
        observer, participant.id, harness.observer, source, clock, TurnAccumulator()
    )
    assert not observer.transcript_identity_lost(participant.id)


async def test_identity_loss_evidence_session_id_matching_another_live_participant_is_rejected(
    collision_registry, vibe_tree
):
    """Loss evidence whose session_id matches another live participant is rejected."""
    observer = Observer(collision_registry, harnesses={})

    # Sibling has a different transcript_location but the same session_id.
    sibling = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="shared-session-id",
    )
    sibling.session_correlation = "exact"
    sibling.transcript_location = str(vibe_tree["transcript_b"])
    collision_registry.store.upsert_participant(sibling)

    participant = collision_registry.register(
        harness="vibe", pane=None, cwd=str(vibe_tree["project"])
    )

    from theater.harness.source import IdentityLossEvidence

    evidence = IdentityLossEvidence(
        location="/some/other/path",
        session_id="shared-session-id",
    )
    assert observer._evidence_is_bound_to_another_live_participant(participant.id, evidence)


async def test_vanished_exact_pin_relocation_matching_a_live_siblings_session_id_is_rejected(
    collision_registry, tmp_path
):
    """A same-session-id relocation candidate (see test_source.py's
    self-heal tests) must still be rejected when that session id already
    belongs to a different live participant, even at an unrelated path --
    and the source must reach quarantine on the next poll rather than retry
    the same rejected candidate forever."""
    root = tmp_path / "projects"
    cwd = str(tmp_path / "work")
    Path(cwd).mkdir(parents=True)
    original = write_claude_transcript(
        root / "-a" / "shared-sid.jsonl", sid="shared-sid", cwd=cwd, text="hello"
    )

    harness = ClaudeCodeHarness(root=root)
    observer = Observer(collision_registry, {"claude": harness})

    participant = collision_registry.register(harness="claude", pane=None, cwd=cwd)
    participant = trust_pin(collision_registry, participant, original, provenance="exact")
    participant.session_id = "shared-sid"
    collision_registry.store.upsert_participant(participant)
    source = await accept_bound_source(observer, harness, participant)

    # A live sibling already owns this exact session id, at an unrelated
    # location -- not the relocation target itself, which is the point: the
    # existing path-ownership check alone would not catch this.
    sibling = collision_registry.register(harness="claude", pane=None, cwd=str(tmp_path / "other"))
    sibling.session_id = "shared-sid"
    sibling.session_correlation = "exact"
    collision_registry.store.upsert_participant(sibling)

    # The harness relocates the pinned file elsewhere under the same root.
    relocated = root / "-b" / "shared-sid.jsonl"
    relocated.parent.mkdir(parents=True)
    original.rename(relocated)

    first = await source.read()
    assert first.attached is not None, "the relocation must actually be found and staged"
    assert first.attached.location == str(relocated)

    assert observer._accept_attachment(participant.id, source, first) is False

    second = await source.read()
    assert second.error_code == TRANSCRIPT_IDENTITY_LOST_CODE, (
        "a rejected relocation must reach quarantine on the next poll, not retry forever"
    )


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
        pane=None,
        cwd=str(vibe_tree["project"]),
    )
    sibling.transcript_location = str(vibe_tree["transcript_b"])
    collision_registry.store.upsert_participant(sibling)

    participant = collision_registry.register(
        harness="vibe", pane=None, cwd=str(vibe_tree["project"])
    )

    from theater.harness.source import IdentityLossEvidence

    evidence = IdentityLossEvidence(location=str(vibe_tree["transcript_b"]))
    assert observer._evidence_is_bound_to_another_live_participant(participant.id, evidence)
