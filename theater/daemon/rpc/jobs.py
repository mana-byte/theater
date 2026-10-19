"""Job await and status RPC handlers, plus the await-bus announcement lifecycle."""

from __future__ import annotations

import asyncio
import logging
import time

from theater.constants.daemon import (
    BUS_KIND_JOB_AWAIT_END,
    BUS_KIND_JOB_AWAIT_START,
    SEND_SUPERSEDED_ERROR_CODE,
    TMUX_RESTART_JOB_ERROR_CODE,
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
from theater.constants.handoff import (
    ERROR_CANCELLED,
    ERROR_LAUNCH_REFUSED,
    ERROR_SOURCE_GONE,
    ERROR_SOURCE_NOT_REVIEWABLE,
)
from theater.constants.observation import (
    COMPLETION_UNVERIFIED_CODE,
    RESCUE_CODE,
    TURN_FAILED_CODE,
    UNDELIVERED_CODE,
)
from theater.constants.response_contract import STRUCTURED_UNABLE_TO_PARSE
from theater.daemon.awaiting import (
    REASON_AWAITING_INPUT,
    AwaitTarget,
    coordinate_await,
    parse_targets,
    snapshot_for,
)
from theater.daemon.controls._common import INTERRUPTED_ERROR_CODE
from theater.daemon.controls.interaction_wire import cached_pending_interaction
from theater.daemon.job_changes import job_changes
from theater.daemon.jobs import STRUCTURED_PARSED, STRUCTURED_UNAVAILABLE
from theater.daemon.rails import CycleDetected, check_cycle, check_wait_cycle
from theater.daemon.response_contracts import audit, parse_stored
from theater.daemon.rpc.params import _finite_number_param, _require
from theater.daemon.rpc.router import method
from theater.json_values import decode_finite_json
from theater.models import BadRequest, Job, JobState, NotFound, Status, new_id
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
    TMUX_RESTART_JOB_ERROR_CODE: (
        "The terminal server restarted and this child is gone; resume it with "
        "spawn_session(resume=<target_id>) using this entry's target_id; the "
        "theater-recover-tmux skill explains."
    ),
    UNDELIVERED_CODE: (
        "Two turns ended without this prompt being seen; read_transcript before re-sending, "
        "since it may never have arrived."
    ),
    SEND_SUPERSEDED_ERROR_CODE: (
        "A newer prompt was accepted after this send's delivery claim expired. This handle was "
        "closed so it cannot consume the newer prompt's completion; await the newer send handle "
        "(replaced_by, when known) instead."
    ),
    INTERRUPTED_ERROR_CODE: (
        "This job was interrupted before it finished: its turn was cancelled, or, as a queued "
        "followup, it was never delivered. Re-send the prompt if the work is still wanted; "
        "replaced_by names the send that followed, if any."
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
    TURN_FAILED_CODE: (
        "The harness reported that the turn failed (see result for the detail). The agent may "
        "still be usable: inspect it before resending."
    ),
    ERROR_SOURCE_NOT_REVIEWABLE: (
        "The deferred review never launched: its source did not leave a clean, pinned commit. "
        "The handoff entry says why; have the source commit, then request the review again."
    ),
    ERROR_SOURCE_GONE: (
        "The deferred task never launched: the job it waited for no longer exists."
    ),
    ERROR_LAUNCH_REFUSED: (
        "The deferred task never launched: the launch was refused once its source ended. "
        "The handoff entry says why."
    ),
    # Several writers share this code; MCP preserves result for their cancellation detail.
    ERROR_CANCELLED: (
        "Cancelled before delivery or launch; nothing reached the target. Request it again if "
        "needed."
    ),
    RESCUE_CODE: (
        "Theater never read this turn's end; after a quiet, idle-looking screen it returned the "
        "last text it heard, which may be interim narration rather than the final answer. "
        "Read the participant's transcript before relying on it."
    ),
    COMPLETION_UNVERIFIED_CODE: (
        "Completion could not be verified: the harness still reported this turn open, or input "
        "queued, behind an idle-looking screen, so no answer was returned. The agent may still "
        "be working; inspect its transcript and pane before resending. A late end of that turn "
        "will not complete a newer job."
    ),
}
#: Error codes whose job a later send may have replaced; window for deriving ``replaced_by``.
_REPLACEABLE = frozenset({INTERRUPTED_ERROR_CODE, SEND_SUPERSEDED_ERROR_CODE})
_REPLACED_BY_WINDOW_SECONDS = 60.0


def _job_to_dict(job: Job, store=None) -> dict:
    """Serialize a job with the actionable explanation for known terminal errors."""
    row = job.to_dict()
    if job.structured_status == STRUCTURED_PARSED:
        valid, _ = decode_finite_json(job.structured_result)
        if not valid:
            row["structured_status"] = (
                STRUCTURED_UNABLE_TO_PARSE
                if parse_stored(job.response_format) is not None
                else STRUCTURED_UNAVAILABLE
            )
            row["structured_result"] = None
    if job.structured_status == STRUCTURED_UNABLE_TO_PARSE and store is not None:
        row["structured_error"] = audit.project(store, job.handle) or {
            "event": "unknown",
            "reason": None,
            "issues": [],
        }
    message = _JOB_ERROR_MESSAGES.get(job.error_code or "")
    if message is not None:
        row["error"] = message
    if store is not None and job.error_code in _REPLACEABLE and job.finished_at is not None:
        # Derived, not stored: the caller's next send to the same target within the window.
        successor = store.first_send_between(
            job.caller_id,
            job.target_id,
            after=job.finished_at,
            until=job.finished_at + _REPLACED_BY_WINDOW_SECONDS,
        )
        if successor is not None and successor.handle != job.handle:
            row["replaced_by"] = successor.handle
    return row


def _quiet_seconds(daemon, pid: str | None) -> float | None:
    """Output silence from the observer's quiet clock, never from last_activity restatements."""
    accessor = getattr(getattr(daemon, "observer", None), "quiet_seconds", None)
    if pid is None or accessor is None:
        return None
    return accessor(pid)


def _entry(daemon, target: AwaitTarget, reasons: dict[str, str]) -> dict:
    """One await result entry: durable job state plus additive presence fields."""
    if target.job is not None:
        entry = _job_to_dict(target.job, daemon.store)
        changes = job_changes(daemon.store, target.job)
        if changes is not None:
            entry["changes"] = changes
        if target.job.state == JobState.RUNNING:
            quiet = _quiet_seconds(daemon, target.job.target_id)
            if quiet is not None:
                entry["quiet_s"] = int(quiet)
    else:
        entry = {"handle": target.handle, "target_id": target.target_id}
    if target.target_id is not None:
        presence = target.presence or snapshot_for(
            getattr(daemon, "presence", None), target.target_id
        )
        entry["human_presence"] = presence.to_dict()
        participant = daemon.store.get_participant(target.target_id)
        entry["participant_status"] = str(participant.status) if participant else None
    else:
        entry["participant_status"] = None
    service = getattr(daemon, "handoff", None)
    handoff = service.describe(target.handle) if service is not None else None
    if handoff is not None:
        entry["handoff"] = handoff
    entry["await_reason"] = reasons.get(target.handle, "timeout")
    if entry["await_reason"] == REASON_AWAITING_INPUT and target.target_id is not None:
        interaction = cached_pending_interaction(daemon, target.target_id)
        if interaction is not None:
            entry["pending_interaction"] = interaction
    return entry


def _check_dependency_cycle(
    daemon, caller_id: str, targets: list[AwaitTarget], dependency_ids: list[str]
) -> None:
    """A gated task starts only after its source job's participant finishes that job.

    Awaiting it therefore also waits on that participant; a loop through it is a real deadlock.
    """
    if not dependency_ids:
        return
    try:
        check_wait_cycle(daemon.jobs.wait_graph, caller_id, dependency_ids)
    except CycleDetected as exc:
        gated = ", ".join(t.handle for t in targets if daemon.handoff.describe(t.handle))
        raise CycleDetected(
            f"awaiting {gated} would deadlock: it starts only after a job of "
            f"{', '.join(dependency_ids)} ends, and that job is waiting on you ({exc}). "
            "End your turn so the source can finish, or await it from a later turn."
        ) from None


def _missing_hint(daemon, handle: str) -> str:
    """Name what an unknown handle most likely meant; never alias it into a target."""
    try:
        participant = daemon.registry.resolve(handle)
    except NotFound:
        participant = None
    if participant is not None and participant.status is not Status.DEAD:
        job = daemon.store.oldest_running_job_for_target(participant.id)
        if job is not None:
            return f"{handle!r} is participant {participant.id}; await its job {job.handle}"
        return (
            f"{handle!r} is participant {participant.id}; it has no running job; "
            "pass its id to wait for presence"
        )
    seq = handle.removeprefix("#")
    if seq.isdigit():
        matches = [
            f"{p.id}#{seq}"
            for p in daemon.store.list_participants(include_dead=True)
            if daemon.jobs.get(f"{p.id}#{seq}") is not None
        ]
        if len(matches) == 1:
            return f"{handle!r} (did you mean {matches[0]}?)"
    return repr(handle)


@method("jobs.await")
async def _jobs_await(daemon, params: dict) -> list[dict]:
    """Wait for job completion, an input request, or departure within max_wait."""
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
    handoff = getattr(daemon, "handoff", None)
    dependency_ids = handoff.dependency_targets([t.handle for t in targets]) if handoff else []
    if caller_id:
        check_cycle(daemon.store, caller_id, target_ids)
        check_wait_cycle(daemon.jobs.wait_graph, caller_id, target_ids)
        _check_dependency_cycle(daemon, caller_id, targets, dependency_ids)

    seen = {t.handle for t in targets}
    missing = [h for h in dict.fromkeys(handles) if h not in seen]
    if missing:
        hints = "; ".join(_missing_hint(daemon, handle) for handle in sorted(missing))
        raise BadRequest(
            f"no such job(s): {hints}. Job handles come only from send/spawn results "
            "(<participant>#<n>) and cannot be predicted before that call returns."
        )

    # One start row per awaited target, only once the call has really blocked;
    # exactly one end row per start, however the await ends. No start, no end.
    await_edges = list(dict.fromkeys((t.handle, t.target_id) for t in targets if t.target_id))
    if not caller_id:
        await_edges = []
    await_token = new_id()
    announced: list[tuple[str, str, float]] = []
    reasons: dict[str, str] = {}
    outcome: str | None = None
    try:
        with daemon.jobs.waiting(caller_id, [*target_ids, *dependency_ids]):
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
    blocked = asyncio.Event()
    started = time.monotonic()
    waiter = asyncio.create_task(
        coordinate_await(daemon, targets, max_wait=max_wait, blocked=blocked)
    )
    blockage = asyncio.create_task(blocked.wait()) if edges else None
    try:
        if blockage is not None:
            finished, _ = await asyncio.wait(
                {waiter, blockage}, return_when=asyncio.FIRST_COMPLETED
            )
            if blockage in finished:
                delay = max(0.0, _await_announce_after() - (time.monotonic() - started))
                if delay == 0:
                    _open_await(daemon, caller_id, edges, token, announced)
                else:
                    finished, _ = await asyncio.wait({waiter}, timeout=delay)
                    if not finished:
                        _open_await(daemon, caller_id, edges, token, announced)
        return await waiter
    finally:
        # A cancelled RPC (the client hung up) must not leave the wait running.
        if not waiter.done():
            waiter.cancel()
        if blockage is not None:
            blockage.cancel()
        await asyncio.gather(
            waiter, *([blockage] if blockage is not None else []), return_exceptions=True
        )


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
    if reason is not None and reason != "job_terminal":
        return reason
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
    return _job_to_dict(job, daemon.store)
