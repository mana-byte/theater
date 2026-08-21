"""Central status policy: QuietClock, _apply, _on_quiet, _settle.

The reducer is the observer's second job — deciding what the text means.
This is where every observation bug in the project has been, and it is not
going to be reimplemented per adapter. The three quiet timers, the screen-
status dispatch, the rescue decision, and the resume-floor suppression all
live here together because they share state and must not be distributed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from theater.daemon import lineage
from theater.daemon.observation.turns import Turn, TurnAccumulator
from theater.harness import (
    Event,
    EventKind,
    ScreenKind,
    status_after,
)
from theater.harness.source import (
    Batch,
)
from theater.models import Status
from theater.models import now as wall_now
from theater.pricing import usage_cost_microcents
from theater.resume_floor import floor_is_present

logger = logging.getLogger("theater.observer")


@dataclass
class QuietClock:
    """How long one participant's watcher has gone without hearing anything.

    Three quiet timers, not one. They measure the same silence but are reset by
    different events, and collapsing them into a single timer is a bug we have
    already shipped once: the relocate fires at 5s and used to reset the clock,
    so the 10s screen check was never reached and AWAITING_INPUT never appeared.
    The rescue timer has the same problem in a worse form — the screen check
    throttles itself by pushing its own clock forward every time it fires, so a
    rescue reading that clock would never come due at all.

    Where the watcher has read *to* is not here any more; that belongs to the
    source, which is the only thing that knows what a position even means for
    its input. This is purely the observer's sense of time passing.
    """

    #: When the participant went quiet, for the relocate timer.
    quiet_since: float | None = None
    #: The same silence, for the screen check. Reset independently.
    screen_quiet_since: float | None = None
    #: The same silence again, for the job rescue. Reset independently.
    rescue_since: float | None = None
    #: Last thing the agent said. What a rescued job returns, since no
    #: turn-end event arrived to carry a result.
    last_text: str = ""

    def stir(self) -> None:
        """Semantic output arrived: every timer starts again from zero."""
        self.quiet_since = None
        self.screen_quiet_since = None
        self.rescue_since = None

    def stir_raw(self) -> None:
        """Input was consumed but produced no event or authoritative status.

        Bytes prove the current source is alive, so relocation and rescue must
        both restart. They say nothing about the participant's rendered state:
        an adapter may be consuming a new record shape it cannot parse, and
        resetting the screen clock here would blind the independent fallback
        for as long as those records keep arriving.
        """
        self.quiet_since = None
        self.rescue_since = None

    def begin_quiet(self, now: float) -> None:
        """Start whichever timers are not already running."""
        if self.quiet_since is None:
            self.quiet_since = now
        if self.screen_quiet_since is None:
            self.screen_quiet_since = now
        if self.rescue_since is None:
            self.rescue_since = now

    def quiet_for(self, now: float) -> float:
        return now - (self.quiet_since if self.quiet_since is not None else now)

    def screen_quiet_for(self, now: float) -> float:
        since = self.screen_quiet_since
        return now - (since if since is not None else now)

    def rescue_quiet_for(self, now: float) -> float:
        since = self.rescue_since
        return now - (since if since is not None else now)


def turn_result(event, turn: Turn) -> tuple[str, str | object | None]:
    """Extract (result_text, raw_result) from a turn-end event."""
    if not (event.text or event.raw_text):
        return turn.said, turn.raw_said
    if event.kind is EventKind.ERROR:
        return event.text, None
    return event.text, event.raw_text if event.raw_text is not None else event.text


def record_usage(pid: str, event: Event, *, store, jobs) -> bool:
    """Persist a usage report, returning whether it was new."""
    assert event.usage is not None
    u = event.usage
    participant = store.get_participant(pid)
    usage_key = u.idempotency_key
    if usage_key is not None and participant is not None:
        scope = participant.session_id or participant.transcript_location
        if scope:
            usage_key = f"{scope}:{usage_key}"
    return store.record_usage(
        participant_id=pid,
        tree_root_id=lineage.root_of(store, pid),
        usage_key=usage_key,
        ts=event.ts if event.ts is not None else wall_now(),
        model=u.model,
        harness=participant.harness if participant is not None else "unknown",
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        cache_creation_input_tokens=u.cache_creation_input_tokens,
        cache_read_input_tokens=u.cache_read_input_tokens,
        reasoning_output_tokens=u.reasoning_output_tokens,
        cost_microcents=usage_cost_microcents(u),
    )


def has_semantic_progress(batch: Batch) -> bool:
    """Whether a batch says something about the participant, not just its source."""
    return (
        any(not event.usage_only for event in batch.events)
        or batch.status is not None
        or batch.attached is not None
    )


def apply_batch(  # noqa: PLR0912
    pid: str,
    batch: Batch,
    clock: QuietClock,
    turns: TurnAccumulator,
    *,
    store,
    registry,
    jobs,
    record_usage_fn,
    settle_fn,
    answer_turn_fn,
    turn_result_fn=turn_result,
) -> bool:
    """Put a batch on the bus and move the participant's status.

    Returns whether anything happened, which is what the quiet timers read.
    A source that consumed input says so with ``progressed``; events and a
    fresh attachment count too.

    Turn ends are answered *inside* the loop, at every boundary. A poll
    drains everything written since the last one, so one batch routinely
    holds a whole turn plus the beginning of the next: ``[assistant(end),
    user]``.

    The turn's text lives in ``turns``, which outlives this call. Batches are
    cut wherever the poll happened to land, so a turn is routinely split
    across two of them, and text accumulated in one must still be there
    when the boundary arrives in the next.
    """
    job_handle: str | None = None
    last = None
    for event in batch.events:
        if event.usage is not None:
            record_usage_fn(pid, event)
        if event.usage_only:
            continue
        store.bus_append(
            f"agent.{event.kind}",
            from_id=pid,
            payload={
                "text": event.text,
                "tool": event.tool_name,
                "ts": event.ts,
                "turn_end": event.turn_end,
                "turn": event.turn_id,
                "index": event.raw_index,
            },
        )
        last = event
        if event.paths:
            if jobs is not None and job_handle is None:
                job = store.oldest_running_job_for_target(pid)
                job_handle = job.handle if job is not None else ""
            if job_handle:
                jobs.observe_paths(job_handle, event.paths)
        if event.kind is EventKind.ASSISTANT and event.text:
            clock.last_text = event.text
            turns.say(event.text, raw_text=event.raw_text)
        if event.kind is EventKind.USER and event.text:
            turns.hear(event.text)
        if event.turn_end:
            turn = turns.take()
            if not turns.already_handled(event.turn_id):
                result_text, raw_result = turn_result_fn(event, turn)
                answer_turn_fn(
                    pid,
                    result_text,
                    turn.heard,
                    raw_result=raw_result,
                )
                turns.mark_handled(event.turn_id)
            clock.last_text = ""

    if batch.status is not None:
        settle_fn(pid, batch.status)
    elif last is not None:
        settle_fn(pid, status_after(last))

    # A non-attachment batch with actual source growth proves the successor
    # has moved beyond a suppressed earlier resume floor. Clear it via
    # targeted update so the participant's own status/last_activity are
    # not reverted. Empty or status-only polls do not clear.
    if batch.attached is None and (batch.progressed or batch.events):
        p_now = store.get_participant(pid)
        if p_now is not None and floor_is_present(p_now.resume_floor):
            store.clear_resume_floor(pid)

    return batch.progressed or bool(batch.events) or batch.attached is not None


def settle(pid: str, desired: Status, *, store, registry) -> None:
    """Move a participant's status, or touch if unchanged."""
    p = store.get_participant(pid)
    if p is None or p.status is Status.DEAD:
        return
    if p.status is desired:
        registry.touch(pid)
    else:
        registry.set_status(pid, desired)


def apply_screen_reading(pid: str, reading, *, store, registry) -> None:
    """Map a screen reading to a status. Applied regardless of confidence."""

    def _settle(p: str, s: Status) -> None:
        settle(p, s, store=store, registry=registry)

    # PROMPT -> IDLE cannot defer to rescue: _rescue_jobs does not touch
    # status, so a participant whose turn ended unobserved would read
    # WORKING forever.
    if reading.kind in (ScreenKind.APPROVAL, ScreenKind.TRUST):
        _settle(pid, Status.AWAITING_INPUT)
        logger.info("participant %s awaiting input (%s on screen)", pid, reading.kind)
    elif reading.kind is ScreenKind.WORKING:
        _settle(pid, Status.WORKING)
    elif reading.kind is ScreenKind.PROMPT:
        _settle(pid, Status.IDLE)
    # UNKNOWN: the screen said nothing the reducer can act on.


def unblock(pid: str, *, store, registry) -> None:
    """New output means the agent is working, whatever the screen said."""
    p = store.get_participant(pid)
    if p and p.status is Status.AWAITING_INPUT:
        registry.set_status(pid, Status.WORKING)
