"""Source errors, quarantine, and identity-loss grace.

When a source reports a contract failure or identity-loss evidence, this
module owns the grace window, bus error publication, and the crash-vs-quarantine
decision. The mutable state (``_source_errors``, ``_identity_lost``) lives on
the Observer instance and is passed in explicitly.
"""

from __future__ import annotations

import logging

from theater.constants.observation import (
    IDENTITY_LOSS_CONFIRMATIONS,
    OBSERVATION_FAILURE_GRACE,
)
from theater.models import JobState, Status
from theater.models import now as wall_now
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    canonical_location,
    same_location,
    transcript_identity_recovery_message,
)

logger = logging.getLogger("theater.observer")


def handle_source_error(
    pid: str,
    batch,
    *,
    store,
    jobs,
    source_errors: dict,
    identity_lost: set,
    bus_append_fn,
    finish_fn,
    wall_now_fn=wall_now,
    grace: float = OBSERVATION_FAILURE_GRACE,
) -> None:
    """Report broken exact correlation and bound affected awaits.

    The source keeps polling, so a late receipt can recover. An old job is
    crashed explicitly rather than waiting forever or falling back to a
    same-cwd transcript that may belong to another process.
    """
    assert batch.error_code is not None
    key = (pid, batch.error_code)
    identity_was_active = pid in identity_lost
    if batch.error_code == TRANSCRIPT_IDENTITY_LOST_CODE:
        participant = store.get_participant(pid)
        if participant is None or participant.status is Status.DEAD:
            return
        identity_lost.add(pid)
    for stale in [item for item in source_errors if item[0] == pid and item != key]:
        source_errors.pop(stale, None)
    failed_at = source_errors.get(key)
    if failed_at is None:
        failed_at = wall_now_fn()
        source_errors[key] = failed_at
        logger.error("observation failed for %s: %s", pid, batch.error or batch.error_code)
        if not (batch.error_code == TRANSCRIPT_IDENTITY_LOST_CODE and identity_was_active):
            bus_append_fn(
                "agent.observation_error",
                to_id=pid,
                payload={"code": batch.error_code, "message": batch.error or ""},
            )
    if jobs is None:
        return
    if batch.error_code == TRANSCRIPT_IDENTITY_LOST_CODE:
        sweep_identity_lost_grace(
            pid,
            failed_at,
            store=store,
            jobs=jobs,
            finish_fn=finish_fn,
            source_errors=source_errors,
            wall_now_fn=wall_now_fn,
            grace=grace,
        )
        return
    now = wall_now_fn()
    for job in store.running_jobs_for_target(pid):
        # A source that failed long ago must not instantly crash a prompt
        # just created against a still-live pane. Give both the channel and
        # the individual job a full chance to recover.
        if now - max(failed_at, job.created_at) >= grace:
            finish_fn(
                job.handle,
                "",
                error_code=batch.error_code,
                state=JobState.CRASHED,
                raw_result=None,
            )


def sweep_identity_lost_grace(
    pid: str,
    failed_at: float | None,
    *,
    store,
    jobs,
    finish_fn,
    source_errors: dict,
    wall_now_fn=wall_now,
    grace: float = OBSERVATION_FAILURE_GRACE,
) -> None:
    """Re-evaluate running jobs against the identity-loss grace window.

    Once a participant is quarantined, ``_watch`` takes the screen-only branch
    forever and the normal source-error path that would crash jobs after grace
    never runs again. This sweep closes that gap.
    """
    if jobs is None:
        return
    key = (pid, TRANSCRIPT_IDENTITY_LOST_CODE)
    if failed_at is None:
        failed_at = source_errors.get(key)
    if failed_at is None:
        return
    now = wall_now_fn()
    for job in store.running_jobs_for_target(pid):
        if now - max(failed_at, job.created_at) >= grace:
            finish_fn(
                job.handle,
                transcript_identity_recovery_message(pid),
                error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
                state=JobState.CRASHED,
                raw_result=None,
            )


def restore_transcript_identity_loss(
    pid: str,
    *,
    store,
    jobs,
    identity_lost: set,
    identity_loss_replayed: set,
    source_errors: dict,
    finish_fn,
    wall_now_fn=wall_now,
    grace: float = OBSERVATION_FAILURE_GRACE,
) -> None:
    """Replay retained audit once for this watcher lifecycle."""
    if pid in identity_loss_replayed:
        return
    identity_loss_replayed.add(pid)
    participant = store.get_participant(pid)
    if participant is None or participant.status is Status.DEAD:
        return
    if not store.observation_error_active(pid, TRANSCRIPT_IDENTITY_LOST_CODE):
        return
    identity_lost.add(pid)
    # Use the persisted bus timestamp so a daemon restart does not reset
    # failed_at to now() and grant endless fresh grace.
    persisted_ts = store.observation_error_timestamp(pid, TRANSCRIPT_IDENTITY_LOST_CODE)
    failed_at = persisted_ts if persisted_ts is not None else wall_now_fn()
    source_errors[(pid, TRANSCRIPT_IDENTITY_LOST_CODE)] = failed_at
    sweep_identity_lost_grace(
        pid,
        failed_at,
        store=store,
        jobs=jobs,
        finish_fn=finish_fn,
        source_errors=source_errors,
        wall_now_fn=wall_now_fn,
        grace=grace,
    )


def clear_source_errors(
    pid: str,
    *,
    source_errors: dict,
    identity_lost: set,
    identity_loss_pending: dict,
    include_identity_lost: bool = False,
) -> None:
    """Remove a participant's source-error entries."""
    for key in [item for item in source_errors if item[0] == pid]:
        if key[1] == TRANSCRIPT_IDENTITY_LOST_CODE and not include_identity_lost:
            continue
        source_errors.pop(key, None)
    if include_identity_lost:
        identity_lost.discard(pid)
        identity_loss_pending.pop(pid, None)


def clear_source_error_on_progress(
    pid: str,
    batch,
    *,
    source_errors: dict,
    identity_lost: set,
    identity_loss_pending: dict,
    reset_identity_loss_confirmation_fn,
) -> None:
    """Clear source errors on actual source progress, not merely a clean empty poll."""
    if batch.error_code is None:
        clear_source_errors(
            pid,
            source_errors=source_errors,
            identity_lost=identity_lost,
            identity_loss_pending=identity_loss_pending,
        )
        # Reset the identity-loss confirmation counter only on actual source
        # progress, not merely on a clean Batch() from a normal empty poll.
        if batch.progressed or bool(batch.events) or batch.attached is not None:
            reset_identity_loss_confirmation_fn(pid)


def confirm_identity_loss(
    pid: str,
    evidence,
    *,
    identity_loss_pending: dict,
    confirmations: int = IDENTITY_LOSS_CONFIRMATIONS,
) -> bool:
    """Require consecutive relocate windows with the same evidence location.

    Returns True when the confirmation threshold is reached.
    """
    pending = identity_loss_pending.get(pid)
    canonical = canonical_location(evidence.location)
    count = (
        pending[1] + 1
        if pending is not None and same_location(pending[0], evidence.location)
        else 1
    )
    identity_loss_pending[pid] = (canonical, count)
    return count >= confirmations


def reset_identity_loss_confirmation(pid: str, *, identity_loss_pending: dict) -> None:
    """Semantic progress on the pinned source resets confirmation."""
    identity_loss_pending.pop(pid, None)
