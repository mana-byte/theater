"""Identity-loss grace, its replay after a restart, and crashing jobs once it expires."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import update

from tests.support.observer_rigs import WaitingHarness
from tests.support.waiting import until
from theater.daemon.jobs import JobManager
from theater.daemon.observer import (
    Observer,
)
from theater.daemon.schema import jobs as jobs_table
from theater.harness.observation import (
    ScreenConfidence,
    ScreenKind,
    ScreenReading,
)
from theater.models import (
    now,
)
from theater.transcript_identity import TRANSCRIPT_IDENTITY_LOST_CODE

# The same failure grace that protects other source errors protects identity-loss job
# destruction: quarantine begins at once, but a job is crashed only once the grace elapsed.
_GRACE_CASES = [
    pytest.param(30.0, "fresh-job", "running", None, id="within-grace-keeps-the-job-running"),
    pytest.param(
        0.0, "old-job", "crashed", TRANSCRIPT_IDENTITY_LOST_CODE, id="after-grace-crashes"
    ),
]


@pytest.mark.parametrize(("grace", "handle", "state", "error_code"), _GRACE_CASES)
async def test_identity_loss_job_destruction_respects_observation_failure_grace(
    registry, grace, handle, state, error_code
):
    """A freshly-created job is not instantly crashed by identity loss.

    ``max(first_failure, job.created_at)`` must elapse before the job is crashed.
    """
    observer = Observer(registry, harnesses={}, failure_grace=grace)
    participant = registry.register(harness="vibe", pane=None, cwd="/tmp")
    manager = JobManager(registry.store)
    observer.jobs = manager
    manager.create(handle=handle, caller_id="caller", target_id=participant.id, kind="send")

    observer.mark_transcript_identity_lost(participant.id, "rotation evidence")

    assert observer.transcript_identity_lost(participant.id)
    job = manager.get(handle)
    assert job.state == state
    if error_code is not None:
        assert job.error_code == error_code


@pytest.mark.parametrize(("grace", "handle", "state", "error_code"), _GRACE_CASES)
async def test_identity_loss_restart_replay_applies_grace_to_job_destruction(
    registry, vibe_tree, grace, handle, state, error_code
):
    """Restart replay quarantines immediately but crashes only jobs that predate the grace."""
    participant = registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    participant.session_id = "deadbeef-1111-2222-3333"
    participant.session_correlation = "operator"
    participant.transcript_location = str(vibe_tree["transcript"])
    registry.store.upsert_participant(participant)

    first = Observer(registry, harnesses={}, failure_grace=grace)
    first.mark_transcript_identity_lost(participant.id, "rotation evidence")

    manager = JobManager(registry.store)
    manager.create(handle=handle, caller_id="caller", target_id=participant.id, kind="send")

    restarted = Observer(registry, harnesses={}, jobs=manager, failure_grace=grace)
    restarted._restore_transcript_identity_loss(participant.id)

    assert restarted.transcript_identity_lost(participant.id)
    job = manager.get(handle)
    assert job.state == state
    if error_code is not None:
        assert job.error_code == error_code


@pytest.mark.asyncio
async def test_identity_loss_grace_sweep_transitions_running_to_crashed(registry, monkeypatch):
    """A job that survives initial quarantine is crashed by the periodic sweep.

    Enter quarantine with positive grace (job stays RUNNING), then advance
    ``failed_at`` backwards so the next sweep tick sees the grace as elapsed
    and crashes the job. This mirrors what happens in the real watch loop:
    the quarantine tick calls ``_sweep_identity_lost_grace`` on every iteration.
    """
    observer = Observer(registry, harnesses={}, failure_grace=30.0)
    participant = registry.register(harness="vibe", pane=None, cwd="/tmp")
    manager = JobManager(registry.store)
    observer.jobs = manager
    manager.create(handle="sweep-job", caller_id="caller", target_id=participant.id, kind="send")

    # Enter quarantine: job is fresh, grace has not elapsed.
    observer.mark_transcript_identity_lost(participant.id, "rotation evidence")
    assert observer.transcript_identity_lost(participant.id)
    assert manager.get("sweep-job").state == "running"

    # Simulate time passing: move the in-memory failed_at backwards AND the
    # job's created_at backwards so the sweep sees the grace as elapsed.
    key = (participant.id, "transcript_identity_lost")
    observer._source_errors[key] = now() - 60.0
    registry.store.conn.execute(
        update(jobs_table).where(jobs_table.c.handle == "sweep-job").values(created_at=now() - 60.0)
    )
    registry.store.conn.commit()

    # The sweep on the next quarantine tick crashes the job.
    observer._sweep_identity_lost_grace(participant.id)
    job = manager.get("sweep-job")
    assert job.state == "crashed"
    assert job.error_code == "transcript_identity_lost"


@pytest.mark.asyncio
async def test_identity_loss_grace_sweep_restart_uses_persisted_timestamp(
    registry, monkeypatch, vibe_tree
):
    """Restart replay uses the persisted bus timestamp, not now(), for the grace window.

    A daemon restart must not grant endless fresh grace by resetting
    ``failed_at`` to ``now()``. The persisted observation-error timestamp
    from the bus is used, so a job that predates the grace window is
    immediately crashed on replay.
    """
    from theater.transcript_identity import TRANSCRIPT_IDENTITY_LOST_CODE

    participant = registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    participant.session_id = "deadbeef-1111-2222-3333"
    participant.session_correlation = "operator"
    participant.transcript_location = str(vibe_tree["transcript"])
    registry.store.upsert_participant(participant)

    first = Observer(registry, harnesses={}, failure_grace=0.0)
    first.mark_transcript_identity_lost(participant.id, "rotation evidence")

    # Verify the bus recorded the observation error with a timestamp.
    persisted_ts = registry.store.observation_error_timestamp(
        participant.id, TRANSCRIPT_IDENTITY_LOST_CODE
    )
    assert persisted_ts is not None

    manager = JobManager(registry.store)
    manager.create(
        handle="persisted-job", caller_id="caller", target_id=participant.id, kind="send"
    )

    restarted = Observer(registry, harnesses={}, jobs=manager, failure_grace=0.0)
    restarted._restore_transcript_identity_loss(participant.id)

    assert restarted.transcript_identity_lost(participant.id)
    # The in-memory failed_at must match the persisted timestamp, not now().
    key = (participant.id, TRANSCRIPT_IDENTITY_LOST_CODE)
    assert restarted._source_errors[key] == persisted_ts
    assert manager.get("persisted-job").state == "crashed"


@pytest.mark.asyncio
async def test_quarantine_watch_branch_crashes_grace_skipped_job(registry, monkeypatch):
    """A job that survives initial quarantine is crashed through the _watch branch.

    This is a real ``_watch``-task test, not a direct helper invocation: the
    quarantine branch calls ``_sweep_identity_lost_grace`` before
    ``_screen_only`` on every tick. With short grace, a grace-skipped job
    transitions RUNNING -> CRASHED through the live watch loop.
    """
    harness = WaitingHarness(ScreenReading(ScreenKind.WORKING, ScreenConfidence.HIGH))
    observer = Observer(
        registry, {"waiting": harness}, poll=0.01, search=0.01, sync=0.01, failure_grace=0.05
    )
    manager = JobManager(registry.store)
    observer.jobs = manager

    async def capture_pane(_pane):
        return "working"

    observer._capture = capture_pane

    participant = registry.register(harness="waiting", pane=None, cwd="/tmp")
    manager.create(handle="watch-job", caller_id="caller", target_id=participant.id, kind="send")

    # Enter quarantine via the first observer (persists to bus).
    first = Observer(registry, harnesses={}, failure_grace=0.05)
    first.mark_transcript_identity_lost(participant.id, "rotation evidence")
    # Replay the persisted quarantine into the live observer's cache.
    observer._restore_transcript_identity_loss(participant.id)
    assert observer.transcript_identity_lost(participant.id)

    # Start the real _watch task. The quarantine branch will run the sweep
    # on every tick; with 0.05s grace, the job is initially RUNNING but
    # crashes after the grace window elapses.
    observer._tasks[participant.id] = asyncio.create_task(
        observer._watch(participant.id, "waiting")
    )
    try:
        # The job starts RUNNING — grace has not elapsed yet.
        assert manager.get("watch-job").state == "running"

        # Wait for the sweep to crash it through the quarantine branch.
        assert await until(lambda: manager.get("watch-job").state == "crashed", timeout=2.0)
        assert manager.get("watch-job").error_code == "transcript_identity_lost"
        # The source was never read — quarantine takes the screen-only branch.
        assert harness.source.reads == 0
    finally:
        await observer.aclose()


def test_identity_lost_predicate_skips_db_for_non_suspects(registry, monkeypatch):
    """Non-suspects return False with no participant SELECT; suspects still check the store."""
    observer = Observer(registry, harnesses={}, failure_grace=30.0)
    participant = registry.register(harness="vibe", pane=None, cwd="/tmp")

    def boom(_pid):
        raise AssertionError("participant read for a non-suspect")

    monkeypatch.setattr(registry.store, "get_participant", boom)
    assert observer.transcript_identity_lost(participant.id) is False
    monkeypatch.undo()

    observer.mark_transcript_identity_lost(participant.id, "rotation evidence")
    reads: list[str] = []
    real = registry.store.get_participant
    monkeypatch.setattr(
        registry.store, "get_participant", lambda pid: (reads.append(pid), real(pid))[1]
    )
    assert observer.transcript_identity_lost(participant.id) is True
    assert reads == [participant.id]
