"""Job await and status RPC handlers, plus the await-bus announcement lifecycle."""

from __future__ import annotations

import asyncio
import logging

from theater.constants.daemon import (
    RPC_AWAIT_ANNOUNCE_DELAY_SECONDS as AWAIT_ANNOUNCE_AFTER,
)
from theater.constants.daemon import (
    RPC_MAX_AWAIT_SECONDS as MAX_AWAIT,
)
from theater.daemon.rails import check_cycle, check_wait_cycle
from theater.daemon.rpc.params import _require
from theater.daemon.rpc.router import method
from theater.models import BadRequest, Job, JobState, new_id
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
)

logger = logging.getLogger(__name__)

_JOB_ERROR_MESSAGES = {
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


@method("jobs.await")
async def _jobs_await(daemon, params: dict) -> list[dict]:
    """Wait for one or more jobs to finish, up to max_wait seconds.

    A handle nobody knows is an error, not an empty list. `await_jobs`
    silently skips what it cannot find, so a typo or a handle from a previous
    daemon used to come back as `[]` — indistinguishable from "nothing to
    report", which sent agents into retry loops against a job that never
    existed.

    The rails run before that complaint. A caller aiming at the wrong end of
    a loop should be told so, whether or not the thing it named turned out to
    be awaitable; "you would deadlock" is the more useful of the two answers.

    Both also run before anything is written to the bus: a call that is refused
    never happened, and must leave no trace for the régie to animate.

    The emission rule, in one place: one `job.await.start` per awaited job that
    names a target, written only once a call from a known caller has been
    blocked for `AWAIT_ANNOUNCE_AFTER` — and exactly one `job.await.end` for
    each start that reached the bus, whether the await returned, timed out, or
    raised. No start, no end.
    """
    handles = params.get("handles") or []
    if not handles:
        raise BadRequest("at least one handle is required")
    max_wait = min(max(float(params.get("max_wait", 150.0)), 0.0), MAX_AWAIT)
    caller_id = params.get("caller_id")

    known = {h: daemon.jobs.get(h) for h in handles}
    # Cycles are about participants, but a send handle is `<target>#<n>`.
    targets = []
    for handle, job in known.items():
        if job is not None:
            if job.target_id:
                targets.append(job.target_id)
        elif daemon.store.get_participant(handle) is not None:
            targets.append(handle)
    if caller_id:
        check_cycle(daemon.store, caller_id, targets)
        check_wait_cycle(daemon.jobs.wait_graph, caller_id, targets)

    missing = [h for h, job in known.items() if job is None]
    if missing:
        raise BadRequest(f"no such job(s): {', '.join(sorted(missing))}")

    # An await is worth announcing only if it can really block.
    known_jobs = [job for job in known.values() if job is not None]
    will_block = max_wait > 0 and all(job.state == JobState.RUNNING for job in known_jobs)
    await_edges: list[tuple[str, str]] = []
    if caller_id and will_block:
        await_edges = [
            (handle, job.target_id)
            for handle, job in known.items()
            if job is not None and job.target_id
        ]

    await_token = new_id()
    announced: list[tuple[str, str]] = []
    try:
        with daemon.jobs.waiting(caller_id, targets):
            jobs = await _await_announced(
                daemon,
                handles=handles,
                max_wait=max_wait,
                caller_id=caller_id,
                edges=await_edges,
                token=await_token,
                announced=announced,
            )
    finally:
        _close_await(daemon, caller_id, announced, await_token)
    rows = []
    for job in jobs:
        row = job.to_dict()
        message = _JOB_ERROR_MESSAGES.get(job.error_code or "")
        if message is not None:
            row["error"] = message
        rows.append(row)
    return rows


async def _await_announced(
    daemon,
    *,
    handles: list[str],
    max_wait: float,
    caller_id: str | None,
    edges: list[tuple[str, str]],
    token: str,
    announced: list[tuple[str, str]],
) -> list[Job]:
    """Wait for the jobs, announcing the wait only if it lasts long enough.

    The wait runs as a task raced against the announce delay rather than being
    preceded by a sleep: an await that is answered in 5ms must still return in
    5ms. What the caller gets back is whatever `await_jobs` returned; what the
    bus gets is a start row per edge, and only once the call has really been
    blocked. `announced` comes from the caller because closing those rows is
    the caller's `finally` — this function can exit by exception too.
    """
    waiter = asyncio.create_task(daemon.jobs.await_jobs(handles, max_wait=max_wait))
    try:
        if edges:
            finished, _ = await asyncio.wait({waiter}, timeout=AWAIT_ANNOUNCE_AFTER)
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
    announced: list[tuple[str, str]],
) -> None:
    """Announce a blocked await, recording every row that reached the bus."""
    for handle, target_id in edges:
        daemon.store.bus_append(
            "job.await.start",
            from_id=caller_id,
            to_id=target_id,
            payload={"handle": handle, "token": token},
        )
        announced.append((handle, target_id))


def _close_await(
    daemon,
    caller_id: str | None,
    announced: list[tuple[str, str]],
    token: str,
) -> None:
    """Close every start row that was written, however the await ended.

    Best effort per row, because this runs in a `finally`.
    """
    for handle, target_id in announced:
        try:
            daemon.store.bus_append(
                "job.await.end",
                from_id=caller_id,
                to_id=target_id,
                payload={"handle": handle, "token": token},
            )
        except Exception:
            logger.exception("could not close await %s on %s", token, handle)


@method("jobs.status")
async def _jobs_status(daemon, params: dict) -> dict:
    """Get the current state of a single job."""
    handle = _require(params, "handle")
    job = daemon.jobs.get(handle)
    if job is None:
        raise BadRequest(f"no job {handle!r}")
    return job.to_dict()
