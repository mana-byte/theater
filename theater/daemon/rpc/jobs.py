"""Job await and status RPC handlers, plus the await-bus announcement lifecycle."""

from __future__ import annotations

import asyncio
import logging
import time

from theater.constants.daemon import (
    BUS_KIND_JOB_AWAIT_END,
    BUS_KIND_JOB_AWAIT_START,
    SEND_SUPERSEDED_ERROR_CODE,
)

# Definitions re-exported by the methods facade; runtime reads the facade for legacy patches.
from theater.constants.daemon import (
    RPC_AWAIT_ANNOUNCE_DELAY_SECONDS as AWAIT_ANNOUNCE_AFTER,  # noqa: F401
)
from theater.constants.daemon import (
    RPC_DEFAULT_MAX_WAIT_SECONDS as DEFAULT_MAX_WAIT,
)
from theater.constants.daemon import (
    RPC_MAX_AWAIT_SECONDS as MAX_AWAIT,  # noqa: F401
)
from theater.daemon.awaiting import (
    AwaitTarget,
    coordinate_await,
    parse_targets,
    snapshot_for,
)
from theater.daemon.rails import check_cycle, check_wait_cycle
from theater.daemon.rpc.params import _finite_number_param, _require
from theater.daemon.rpc.router import method
from theater.models import BadRequest, Job, JobState, new_id
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
)

logger = logging.getLogger(__name__)


def _max_await() -> float:
    from theater.daemon import methods as _facade

    return _facade.MAX_AWAIT


def _await_announce_after() -> float:
    from theater.daemon import methods as _facade

    return _facade.AWAIT_ANNOUNCE_AFTER


_JOB_ERROR_MESSAGES = {
    SEND_SUPERSEDED_ERROR_CODE: (
        "A newer prompt was accepted after this send's delivery claim expired. This handle was "
        "closed so it cannot consume the newer prompt's completion; await the newer send handle "
        "instead."
    ),
    "transcript_correlation_failed": (
        "Theater could not correlate this participant with its transcript. "
        "The agent may still be alive and working; do not retry the task, and inspect "
        "its pane before deciding what to do."
    ),
    "transcript_correlation_ambiguous": (
        "Theater found transcript output that is not uniquely attributable to this "
        "participant. The agent may still be alive and working; do not retry the task, "
        "and inspect its pane before deciding what to do."
    ),
    TRANSCRIPT_IDENTITY_LOST_CODE: (
        "Theater lost the trusted transcript identity for this participant. Screen status "
        "may still be live, but transcript attribution is quarantined; inspect candidates "
        "and rebind the participant before sending more work."
    ),
    TRANSCRIPT_SOURCE_UNAVAILABLE_CODE: (
        "The transcript source stayed unavailable past the observation grace. The pane may "
        "still be healthy; inspect it before retrying or replacing any binding."
    ),
}


def _job_to_dict(job: Job) -> dict:
    """Serialize a job with the actionable explanation for known terminal errors."""
    row = job.to_dict()
    message = _JOB_ERROR_MESSAGES.get(job.error_code or "")
    if message is not None:
        row["error"] = message
    return row


def _entry(daemon, target: AwaitTarget, reasons: dict[str, str]) -> dict:
    """One await result entry: durable job state plus additive presence fields."""
    if target.job is not None:
        entry = _job_to_dict(target.job)
    else:
        entry = {"handle": target.handle, "target_id": target.target_id}
    if target.target_id is not None:
        provider = getattr(daemon, "presence", None)
        entry["human_presence"] = snapshot_for(provider, target.target_id).to_dict()
        participant = daemon.store.get_participant(target.target_id)
        entry["participant_status"] = str(participant.status) if participant else None
    else:
        entry["participant_status"] = None
    entry["await_reason"] = reasons.get(target.handle, "timeout")
    return entry


@method("jobs.await")
async def _jobs_await(daemon, params: dict) -> list[dict]:
    """Wait for jobs, or for a human to leave, up to max_wait seconds."""
    # A handle nobody knows is an error: `[]` sent agents into retry loops.
    # Presence-only handles (a participant id with no job) are legitimate now.
    handles = params.get("handles") or []
    if not handles:
        raise BadRequest("at least one handle is required")
    raw_max_wait = params.get("max_wait", DEFAULT_MAX_WAIT)
    max_wait = min(
        max(_finite_number_param(raw_max_wait, "max_wait", method_name="jobs.await"), 0.0),
        _max_await(),
    )
    caller_id = params.get("caller_id")

    targets = parse_targets(daemon, handles)
    # Rails before the unknown-handle complaint and before any bus row.
    target_ids = [t.target_id for t in targets if t.target_id]
    if caller_id:
        check_cycle(daemon.store, caller_id, target_ids)
        check_wait_cycle(daemon.jobs.wait_graph, caller_id, target_ids)

    seen = {t.handle for t in targets}
    missing = [h for h in dict.fromkeys(handles) if h not in seen]
    if missing:
        raise BadRequest(f"no such job(s): {', '.join(sorted(missing))}")

    # One start row per awaited target, only once the call has really blocked;
    # exactly one end row per start, however the await ends. No start, no end.
    await_edges = [(t.handle, t.target_id) for t in targets if t.target_id]
    await_token = new_id()
    announced: list[tuple[str, str, float]] = []
    reasons: dict[str, str] = {}
    outcome: str | None = None
    try:
        with daemon.jobs.waiting(caller_id, target_ids):
            try:
                reasons = await _await_announced(
                    daemon,
                    targets=targets,
                    max_wait=max_wait,
                    caller_id=caller_id,
                    edges=await_edges,
                    token=await_token,
                    announced=announced,
                )
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            except BaseException:
                outcome = "error"
                raise
    finally:
        _close_await(
            daemon,
            caller_id,
            announced,
            await_token,
            state=outcome,
            targets=targets,
            reasons=reasons,
        )
    return [_entry(daemon, target, reasons) for target in targets]


async def _await_announced(
    daemon,
    *,
    targets: list[AwaitTarget],
    max_wait: float,
    caller_id: str | None,
    edges: list[tuple[str, str]],
    token: str,
    announced: list[tuple[str, str, float]],
) -> dict[str, str]:
    """Run the wait, announcing it only if it lasts past the delay."""
    # Racing the delay keeps a 5ms answer a 5ms answer.
    waiter = asyncio.create_task(coordinate_await(daemon, targets, max_wait=max_wait))
    try:
        if edges:
            finished, _ = await asyncio.wait({waiter}, timeout=_await_announce_after())
            if not finished:
                _open_await(daemon, caller_id, edges, token, announced)
        return await waiter
    finally:
        # A cancelled RPC (the client hung up) must not leave the wait running.
        if not waiter.done():
            waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


def _open_await(
    daemon,
    caller_id: str | None,
    edges: list[tuple[str, str]],
    token: str,
    announced: list[tuple[str, str, float]],
) -> None:
    """Announce a blocked await, recording every row that reached the bus."""
    for handle, target_id in edges:
        daemon.store.bus_append(
            BUS_KIND_JOB_AWAIT_START,
            from_id=caller_id,
            to_id=target_id,
            payload={"handle": handle, "token": token},
        )
        announced.append((handle, target_id, time.monotonic()))


def _close_await(
    daemon,
    caller_id: str | None,
    announced: list[tuple[str, str, float]],
    token: str,
    *,
    state: str | None,
    targets: list[AwaitTarget],
    reasons: dict[str, str],
) -> None:
    """Close every start row that was written, however the await ended."""
    # Best effort per row, because this runs in a `finally`.
    jobs_by_handle = {t.handle: t.job for t in targets if t.job is not None}
    for handle, target_id, started_at in announced:
        try:
            job_state = state
            if job_state is None:
                job_state = _bus_end_state(jobs_by_handle.get(handle), reasons.get(handle))
            daemon.store.bus_append(
                BUS_KIND_JOB_AWAIT_END,
                from_id=caller_id,
                to_id=target_id,
                payload={
                    "handle": handle,
                    "token": token,
                    "state": job_state,
                    "elapsed_seconds": max(0.0, time.monotonic() - started_at),
                },
            )
        except Exception:
            logger.exception("could not close await %s on %s", token, handle)


def _bus_end_state(job: Job | None, reason: str | None) -> str:
    """The bus row's state: durable outcome for jobs, else the await reason."""
    if job is not None:
        if job.state == JobState.DONE:
            return "completed"
        if job.state != JobState.RUNNING:
            return "error"
    return reason or "timeout"


@method("jobs.status")
async def _jobs_status(daemon, params: dict) -> dict:
    """Get the current state of a single job."""
    handle = _require(params, "handle")
    job = daemon.jobs.get(handle)
    if job is None:
        raise BadRequest(f"no job {handle!r}")
    return _job_to_dict(job)
