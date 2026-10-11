"""``send(if_busy=...)``: redirect a busy child through the existing queue, steer, interrupt."""

from __future__ import annotations

import asyncio
import time
from typing import Final

from theater.constants.refusals import NEXT_REPLACE_TIMEOUT
from theater.daemon.controls._common import ACTION_INTERRUPT
from theater.models import BadRequest, Busy, StaleTarget, TheaterError

#: "auto" is the default: Theater delivers when safe and queues otherwise. The rest are explicit.
IF_BUSY_MODES: Final = ("auto", "refuse", "queue", "amend", "replace")
#: Replace waits this long for an observed idle; it stays under the client CALL_TIMEOUT (40 s).
REPLACE_IDLE_WAIT_SECONDS: Final = 30.0
_REPLACE_POLL_SECONDS: Final = 0.2
_CARD_MAX_JOBS: Final = 10


class ReplaceTimeout(TheaterError):
    """The interrupted child did not become idle in time; nothing was sent."""

    code = "replace_timeout"
    details = {"next": NEXT_REPLACE_TIMEOUT}  # noqa: RUF012 - read-only default; never mutated


def if_busy_param(params: dict) -> str:
    value = params.get("if_busy") or "auto"
    if value not in IF_BUSY_MODES:
        raise BadRequest(
            f"send parameter 'if_busy' must be one of {', '.join(IF_BUSY_MODES)}; got {value!r}"
        )
    return value


def authorize_redirect(daemon, target_id: str, caller_id: str) -> None:
    """Every mode but refuse may queue, steer, or interrupt: direct parent or operator only."""
    daemon.controls._gates.authorize(target_id, caller_id, ACTION_INTERRUPT)


def require_active_job(daemon, target_id: str, job_handle: str | None) -> str | None:
    """The job_handle guard: act only while that job is the target's active turn."""
    from theater.daemon.rpc.interruption import active_job_handle

    active = active_job_handle(daemon, target_id)
    if job_handle is not None and active != job_handle:
        raise StaleTarget(
            f"participant {target_id!r} is running job {active!r}, not {job_handle!r}; "
            "refusing to redirect a different job than the one you expect"
        )
    return active


async def queue(daemon, params: dict) -> dict:
    from theater.daemon.rpc.controls import _queue_followup

    result = await _queue_followup(daemon, params)
    result["route"] = "queued"
    return result


async def amend(daemon, params: dict, *, send) -> dict:
    """Steer when offered; only a definite refusal falls back to send or queue."""
    from theater.daemon.rpc.controls import _steer

    target_id = daemon.registry.resolve(params["target"]).id
    job_handle = params.get("job_handle")
    require_active_job(daemon, target_id, job_handle)
    steer_params = {k: params[k] for k in ("target", "prompt", "caller_id") if k in params}
    try:
        result = await _steer(daemon, {**steer_params, "job_handle": job_handle})
    except (StaleTarget, BadRequest) as exc:
        # Raised before delivery or on a REJECTED receipt; UNKNOWN returns instead.
        if job_handle is not None:
            raise
        try:
            return await send()
        except Busy:
            result = await queue(daemon, params)
            result["steer_refused"] = exc.code
            return result
    result["route"] = "steer"
    return result


def _caller_name(daemon, caller_id: str) -> str:
    if caller_id == "cli":
        return "the operator"
    participant = daemon.store.get_participant(caller_id)
    return (participant.name if participant else None) or caller_id


def _running_launched(daemon, target_id: str) -> list[tuple[str, str, str]]:
    rows = []
    for job in daemon.store.running_jobs_for_caller(target_id, limit=_CARD_MAX_JOBS):
        participant = daemon.store.get_participant(job.target_id)
        name = (participant.name if participant else None) or job.target_id
        state = str(participant.status) if participant else "unknown"
        rows.append((job.handle, name, state))
    return rows


async def replace(daemon, params: dict, *, send) -> dict:
    """Cancel the queue, interrupt, wait for an observed idle, then send with a context card."""
    from theater.daemon.rpc.interruption import _interrupt
    from theater.daemon.rpc.params import _prompt_with_interruption_card

    deadline = time.monotonic() + REPLACE_IDLE_WAIT_SECONDS
    caller_id = params["caller_id"]
    target_id = daemon.registry.resolve(params["target"]).id
    require_active_job(daemon, target_id, params.get("job_handle"))
    running = _running_launched(daemon, target_id)
    interrupted = await _interrupt(daemon, {"target": target_id, "caller_id": caller_id})
    interrupted_job = interrupted.get("interrupted_job")
    card = {
        "interrupted": interrupted_job,
        "by": _caller_name(daemon, caller_id),
        "running": running,
    }
    while True:
        try:
            job = await send(lambda prompt: _prompt_with_interruption_card(prompt, **card))
            break
        except Busy:
            if time.monotonic() >= deadline:
                raise ReplaceTimeout(
                    f"participant {target_id!r} was interrupted but was not observed idle "
                    f"within {REPLACE_IDLE_WAIT_SECONDS:.0f} s; the interrupt stays applied "
                    "and nothing was sent — await the interrupted handle, then send again"
                ) from None
            await asyncio.sleep(_REPLACE_POLL_SECONDS)
    job["route"] = "replace"
    if interrupted_job is not None:
        job["interrupted_job"] = interrupted_job
    job["cancelled_followups"] = interrupted.get("cancelled_followups", [])
    return job
