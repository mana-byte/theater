"""Identity loss needs consecutive windows at one location; the grace sweep crashes stale jobs."""

from __future__ import annotations

import os
import time

from tests.support.collision_rig import accept_bound_source, make_session, trust_pin
from tests.support.collision_rig import collision_registry as shared_collision_registry
from tests.support.collision_rig import vibe_tree as shared_vibe_tree
from tests.support.shipped import VibeHarness
from theater.daemon.jobs import JobManager
from theater.daemon.observer import Observer, QuietClock, TurnAccumulator
from theater.harness.observation import ScreenConfidence, ScreenKind, ScreenReading
from theater.harness.source import Batch

vibe_tree = shared_vibe_tree  # the shared fixture, under the name the tests ask for
collision_registry = (
    shared_collision_registry  # the shared fixture, under the name the tests ask for
)


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
        harness="vibe", pane=None, cwd=str(vibe_tree["project"])
    )
    participant = trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    source = await accept_bound_source(observer, harness, participant)
    make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))

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
        harness="vibe", pane=None, cwd=str(vibe_tree["project"])
    )
    participant = trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    source = await accept_bound_source(observer, harness, participant)
    make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))

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
        harness="vibe", pane=None, cwd=str(vibe_tree["project"])
    )
    participant = trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    source = await accept_bound_source(observer, harness, participant)
    first_new = make_session(vibe_tree["root"], "yyyyyyyy", str(vibe_tree["project"]))

    clock = QuietClock()
    clock.quiet_since = time.monotonic() - 10
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    assert observer._identity_loss_pending[participant.id] == (str(first_new), 1)

    # A different candidate location resets the counter to 1, not 2.
    second_new = make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))
    clock.quiet_since = time.monotonic() - 10
    await observer._on_quiet(participant.id, harness.observer, source, clock, TurnAccumulator())
    assert not observer.transcript_identity_lost(participant.id)
    assert observer._identity_loss_pending[participant.id] == (str(second_new), 1)


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
    jobs.create(handle="sweep-job", caller_id="caller", target_id=participant.id, kind="send")
    make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))

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
    observer = Observer(
        collision_registry, {"vibe": harness}, relocate=0.0, jobs=jobs, failure_grace=30.0
    )

    async def capture_pane(_pane):
        return "working"

    observer._capture = capture_pane
    participant = collision_registry.register(
        harness="vibe", pane=None, cwd=str(vibe_tree["project"])
    )
    participant = trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    source = await accept_bound_source(observer, harness, participant)
    jobs.create(handle="fresh-job", caller_id="caller", target_id=participant.id, kind="send")
    make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))

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

    participant = collision_registry.register(
        harness="vibe", pane=None, cwd=str(vibe_tree["project"])
    )
    participant = trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    # Mark identity loss with the first observer.
    first = Observer(collision_registry, harnesses={}, jobs=jobs, failure_grace=0.0)
    first.mark_transcript_identity_lost(participant.id, "rotation evidence")
    assert first.transcript_identity_lost(participant.id)

    # A job created after the first observer's quarantine.
    jobs.create(handle="restart-job", caller_id="caller", target_id=participant.id, kind="send")

    # Restart: the new observer replays from the persisted bus timestamp.
    # With 0 grace, the sweep should immediately crash the job because
    # the persisted failed_at predates the job's creation.
    restarted = Observer(collision_registry, harnesses={}, jobs=jobs, failure_grace=0.0)
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
    pin = make_session(root, "aaaaaaa", str(project), text="mine")
    os.utime(pin, ns=(1_000_000_000, 1_000_000_000))

    harness = VibeHarness(root=root)
    harness.observer.screen_reading = lambda _capture: ScreenReading(  # type: ignore[method-assign]
        ScreenKind.WORKING, ScreenConfidence.HIGH
    )
    observer = Observer(collision_registry, {"vibe": harness}, relocate=0.0)

    async def capture_pane(_pane):
        return "working"

    observer._capture = capture_pane
    participant = collision_registry.register(harness="vibe", pane=None, cwd=str(project))
    participant = trust_pin(collision_registry, participant, pin)
    source = await accept_bound_source(observer, harness, participant)

    # Create a newer candidate.
    candidate = make_session(root, "zzzzzzzz", str(project), text="not mine")
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
    candidate2 = make_session(root, "zzzzzzzz", str(project), text="not mine")
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
        harness="vibe", pane=None, cwd=str(vibe_tree["project"])
    )
    participant = trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    source = await accept_bound_source(observer, harness, participant)
    make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))

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
        harness="vibe", pane=None, cwd=str(vibe_tree["project"])
    )
    participant = trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    source = await accept_bound_source(observer, harness, participant)
    make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))

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
        harness="vibe", pane=None, cwd=str(vibe_tree["project"])
    )
    participant = trust_pin(collision_registry, participant, vibe_tree["transcript_a"])
    source = await accept_bound_source(observer, harness, participant)
    make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))

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
