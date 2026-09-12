"""Presence-aware await coordination for the jobs.await RPC."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from theater.daemon.presence.contracts import PresenceProvider, PresenceSnapshot, PresenceState
from theater.models import Job, JobState

logger = logging.getLogger("theater.awaiting")

#: Await outcome label placed on every result entry.
REASON_JOB_TERMINAL = "job_terminal"
REASON_PRESENCE_RELEASED = "presence_released"
REASON_ALREADY_ABSENT = "already_absent"
REASON_TIMEOUT = "timeout"
REASON_PENDING = "pending"

_MISSING = PresenceSnapshot(
    state=PresenceState.UNKNOWN,
    reason="presence provider not composed",
    revision=-1,
    observed_at=None,
)
_ERRORED = PresenceSnapshot(
    state=PresenceState.UNKNOWN,
    reason="presence snapshot error",
    revision=-1,
    observed_at=None,
)


@dataclass
class AwaitTarget:
    """One awaited handle: its durable job (or None) and its presence target.

    Presence decides gating exactly once, at admission: the first snapshot the
    first evaluation reads (fail-closed when refreshed facts are missing or
    errored). A target gated at admission then needs one observed departure
    AND one observed terminal job state, in either order; a target admitted
    unprotected never consults presence again.
    """

    handle: str
    target_id: str | None
    job: Job | None
    reason: str | None = None
    presence: PresenceSnapshot | None = None
    #: Admission gate — recorded exactly once by the first evaluation pass and
    #: never rewritten. True iff the admission snapshot was protected (present,
    #: unknown, missing provider, or failed refresh — all fail-closed). None
    #: means "not yet admitted"; the first _evaluate pass always runs before
    #: any wait round.
    admission_protected: bool | None = None
    #: Sticky — an *observed* unprotected snapshot after admission cleared the
    #: gate. Never unset. Arrivals and coalesced bursts that land on a
    #: protected snapshot never set it.
    departure_observed: bool = False
    #: Sticky — the job was observed in a terminal state (done/crashed/killed).
    #: Never unset. Only meaningful for job targets.
    terminal_observed: bool = False
    #: Which sticky condition transitioned last: REASON_JOB_TERMINAL or
    #: REASON_PRESENCE_RELEASED. Updated only on a False→True transition; a
    #: dual transition inside one evaluation records REASON_JOB_TERMINAL —
    #: the deterministic tie-break, never a timestamp comparison.
    last_condition: str | None = None


def parse_targets(daemon, handles: list[str]) -> list[AwaitTarget]:
    """Resolve handles in order; job handles win over participant ids."""
    targets: list[AwaitTarget] = []
    for handle in handles:
        job = daemon.jobs.get(handle)
        if job is not None:
            targets.append(AwaitTarget(handle=handle, target_id=job.target_id, job=job))
            continue
        participant = daemon.store.get_participant(handle)
        if participant is None:
            continue
        targets.append(AwaitTarget(handle=handle, target_id=participant.id, job=None))
    return targets


def snapshot_for(provider: PresenceProvider | None, participant_id: str) -> PresenceSnapshot:
    """Cached provider lookup; missing provider or error stays protected."""
    if provider is None:
        return _MISSING
    try:
        return provider.snapshot(participant_id)
    except Exception:
        logger.exception("presence snapshot failed for %s", participant_id)
        return _ERRORED


def _record_admission_and_transitions(target: AwaitTarget, protected: bool) -> None:
    """Latch the one-shot admission gate and this pass's sticky transitions.

    Admission is recorded exactly once, on the first evaluation pass, from
    the (possibly errored or missing — fail-closed) admission snapshot. A
    departure counts only when an evaluation actually observes an
    unprotected snapshot; arrivals and coalesced bursts that land protected
    never clear the gate.
    """
    if target.admission_protected is None:
        target.admission_protected = protected
    terminal_now = target.job is not None and target.job.state != JobState.RUNNING
    terminal_transition = terminal_now and not target.terminal_observed
    if terminal_transition:
        target.terminal_observed = True
    departure_transition = (
        bool(target.admission_protected) and not protected and not target.departure_observed
    )
    if departure_transition:
        target.departure_observed = True
    if terminal_transition or departure_transition:
        # Whichever sticky condition transitioned last wins the reported
        # reason; a same-pass dual transition is the deterministic tie-break,
        # never a timestamp comparison.
        target.last_condition = (
            REASON_PRESENCE_RELEASED
            if departure_transition and not terminal_transition
            else REASON_JOB_TERMINAL
        )


def _qualifying_reason(target: AwaitTarget) -> str | None:
    """The qualifying reason from sticky state, or None while still waiting."""
    if target.job is not None:
        if not target.admission_protected:
            # Admitted unprotected: terminal state alone qualifies; a human
            # arriving later — or leaving later — is irrelevant.
            return REASON_JOB_TERMINAL if target.terminal_observed else None
        # Gated: both conditions, either order, gate cleared for good.
        if target.departure_observed and target.terminal_observed:
            return target.last_condition
        return None
    if not target.admission_protected:
        return REASON_ALREADY_ABSENT
    return REASON_PRESENCE_RELEASED if target.departure_observed else None


def _evaluate(
    daemon,
    provider: PresenceProvider | None,
    targets: list[AwaitTarget],
    *,
    failed: bool = False,
) -> bool:
    """Refresh jobs and presence; mark per-target qualifying reasons.

    Presence decides gating exactly once, at admission. After that, an
    admission-gated target needs one observed departure AND one observed
    terminal job state, in either order; a target admitted unprotected never
    consults presence again, so a human arriving later — or leaving later —
    cannot release a still-running job or hold a finished one.
    """
    qualified = False
    for target in targets:
        if target.job is not None:
            target.job = daemon.jobs.get(target.handle) or target.job
        if target.target_id is not None:
            target.presence = _ERRORED if failed else snapshot_for(provider, target.target_id)
        protected = target.presence is not None and target.presence.protected
        _record_admission_and_transitions(target, protected)
        target.reason = _qualifying_reason(target)
        if target.reason is not None:
            qualified = True
    return qualified


def _reasons(targets: list[AwaitTarget], qualified: bool) -> dict[str, str]:
    """Per-handle outcome: qualifying reasons, else pending or timeout."""
    reasons: dict[str, str] = {}
    for target in targets:
        if qualified and target.reason is not None:
            reasons[target.handle] = target.reason
        elif qualified:
            reasons[target.handle] = REASON_PENDING
        else:
            reasons[target.handle] = REASON_TIMEOUT
    return reasons


def _running_job_handles(daemon, targets: list[AwaitTarget]) -> list[str]:
    """Handles of targets that still have a running job to wait on."""
    return [t.handle for t in targets if t.job is not None and t.job.state == JobState.RUNNING]


def _waiter_progressed(daemon, handles: list[str]) -> bool:
    """True when one of the awaited jobs actually became terminal."""
    for handle in handles:
        job = daemon.jobs.get(handle)
        if job is not None and job.state != JobState.RUNNING:
            return True
    return False


def _armed(*tasks: asyncio.Task | None) -> set[asyncio.Task]:
    """The non-None task subset: the wake set for one wait round."""
    return {task for task in tasks if task is not None}


def _arm_waiter(daemon, handles: list[str], ceiling: float | None, remaining: float):
    """One jobs waiter; the first arm carries the exact capped ceiling."""
    if not handles:
        return None, ceiling
    budget = ceiling if ceiling is not None else remaining
    return asyncio.create_task(daemon.jobs.await_jobs(handles, max_wait=budget)), None


async def _refresh(provider: PresenceProvider | None, deadline: float) -> bool:
    """Refresh inside the caller's deadline; errors invalidate cached absence."""
    if provider is None:
        return False
    try:
        async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
            await provider.refresh()
    except Exception:
        logger.debug("presence refresh failed or timed out; holding protected", exc_info=True)
        return False
    return True


async def _teardown(*tasks: asyncio.Task | None) -> None:
    """Cancel and drain the background waiter and presence subscription."""
    live = _armed(*tasks)
    for task in live:
        task.cancel()
    await asyncio.gather(*live, return_exceptions=True)


def _presence_settled(task: asyncio.Task) -> bool:
    """Consume a finished presence wait; False means the subscription broke."""
    try:
        task.result()
    except Exception:
        logger.exception("presence subscription failed; holding protected targets")
        return False
    return True


async def coordinate_await(
    daemon,
    targets: list[AwaitTarget],
    *,
    max_wait: float,
    blocked: asyncio.Event | None = None,
) -> dict[str, str]:
    """Wait until any target qualifies, or the single deadline expires."""
    blocked = blocked or asyncio.Event()
    deadline = time.monotonic() + max_wait
    provider = getattr(daemon, "presence", None)
    failed = max_wait > 0 and not await _refresh(provider, deadline)
    # The first waiter gets the exact capped ceiling; later ones the remainder.
    wait_budget: float | None = max_wait
    waiter: asyncio.Task | None = None
    waiter_handles: list[str] = []
    presence_task: asyncio.Task | None = None
    presence_broken = False
    try:
        while True:
            if _evaluate(daemon, provider, targets, failed=failed or presence_broken):
                return _reasons(targets, qualified=True)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _reasons(targets, qualified=False)
            blocked.set()
            if waiter is None:
                waiter_handles = _running_job_handles(daemon, targets)
                waiter, wait_budget = _arm_waiter(daemon, waiter_handles, wait_budget, remaining)
            if presence_task is None and provider is not None and not presence_broken:
                after_revision = provider.revision
                presence_task = asyncio.create_task(provider.wait_for_change(after_revision))
            wake = _armed(waiter, presence_task)
            if not wake:
                await asyncio.sleep(remaining)
                return _reasons(targets, qualified=False)
            done, _ = await asyncio.wait(
                wake, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                return _reasons(targets, qualified=False)
            if waiter is not None and waiter in done:
                waiter.result()
                waiter = None
                if not _waiter_progressed(daemon, waiter_handles):
                    # await_jobs hit its own ceiling: the deadline is here.
                    return _reasons(targets, qualified=False)
            if presence_task is not None and presence_task in done:
                presence_broken = not _presence_settled(presence_task)
                presence_task = None
                if failed and not presence_broken:
                    failed = not await _refresh(provider, deadline)
    finally:
        await _teardown(waiter, presence_task)
