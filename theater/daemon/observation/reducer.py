"""Central status policy: QuietClock, _apply, _on_quiet, _settle, screen status.

``_on_quiet`` ordering matters: relocate -> identity probe -> screen status -> rescue.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass

from sqlalchemy import Connection

from theater.constants.observation import (
    LAST_ACTIVITY_REFRESH_SECONDS,
    SCREEN_CHECK_BACKOFF_CAP_SECONDS,
    WORKING_SCREEN_INTERVAL_CAP_SECONDS,
)
from theater.daemon import lineage
from theater.daemon.observation.rollback import rollback_observation_state
from theater.daemon.observation.screen import end_turn_from_screen_text
from theater.daemon.observation.turns import Turn, TurnAccumulator
from theater.daemon.persistence.transactions import active_write_unit
from theater.harness import Event, EventKind, HarnessObserver, ScreenKind, status_after
from theater.harness.source import Batch, Source
from theater.models import Status
from theater.pricing import usage_cost_microcents
from theater.resume_floor import floor_is_present

logger = logging.getLogger("theater.observer")


@dataclass(frozen=True, slots=True)
class _ApplyCallbacks:
    answer_turn: Callable
    turn_result: Callable
    path_target: Callable | None


@dataclass(slots=True)
class _ApplyState:
    job_handle: str | None = None
    last: Event | None = None
    observed_at: float | None = None
    usage_events: list[Event] | None = None


@dataclass(frozen=True, slots=True)
class _ApplyContext:
    pid: str
    clock: QuietClock
    turns: TurnAccumulator
    callbacks: _ApplyCallbacks
    state: _ApplyState
    connection: Connection | None
    #: A terminal batch finishes its job in the same unit, so touches must be seen first.
    paths_now: bool = False
    #: Source read time of the batch, stored on its bus payloads for observe_to_bus.
    read_at: float | None = None


@dataclass
class QuietClock:
    """How long one participant's watcher has gone without hearing anything.

    Three quiet timers, not one: the same silence reset by different events. Collapsing them
    is a bug we already shipped once; the rescue timer has the same problem in a worse form.
    """

    quiet_since: float | None = None
    screen_quiet_since: float | None = None
    rescue_since: float | None = None
    last_text: str = ""
    screen_backoff: int = 0
    last_screen_kind: ScreenKind | None = None

    def stir(self) -> None:
        """Semantic output arrived: every timer starts again from zero."""
        self.quiet_since = None
        self.screen_quiet_since = None
        self.rescue_since = None
        self.screen_backoff = 0
        self.last_screen_kind = None

    def stir_raw(self) -> None:
        """Input was consumed but produced no event or authoritative status."""
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

    def screen_interval(self, base: float) -> float:
        """The period: doubles while a PROMPT holds, then caps; WORKING never exceeds its budget.

        A WORKING reading gates sends, so its approval flip must land inside the target:
        interval + slowest tick + capture. Both gates in the reducer read this one value.
        """
        if self.last_screen_kind is ScreenKind.WORKING:
            return min(base, WORKING_SCREEN_INTERVAL_CAP_SECONDS)
        if self.last_screen_kind is not ScreenKind.PROMPT:
            return base
        return max(base, min(base * 2**self.screen_backoff, SCREEN_CHECK_BACKOFF_CAP_SECONDS))

    def note_screen(self, kind: ScreenKind | None) -> None:
        """A changed reading restarts the period; an unchanged one stretches it."""
        self.screen_backoff = (
            min(self.screen_backoff + 1, 8) if kind == self.last_screen_kind else 0
        )
        self.last_screen_kind = kind

    def quiet_for(self, now: float) -> float:
        return now - (self.quiet_since if self.quiet_since is not None else now)

    def screen_quiet_for(self, now: float) -> float:
        since = self.screen_quiet_since
        return now - (since if since is not None else now)

    def rescue_quiet_for(self, now: float) -> float:
        since = self.rescue_since
        return now - (since if since is not None else now)


class Reducer:
    """Owns the quiet-timer policy, status dispatch, and batch application."""

    def __init__(
        self,
        store,
        registry,
        *,
        wall_now_fn,
        capture_fn,
        monotonic_fn,
        config_fn,
        jobs_fn,
        telemetry_fn=None,
    ):
        self.store = store
        self.registry = registry
        self._wall_now_fn = wall_now_fn
        self._capture_fn = capture_fn
        self._monotonic_fn = monotonic_fn
        self._config_fn = config_fn
        self._jobs_fn = jobs_fn
        self._telemetry_fn = telemetry_fn

    @property
    def jobs(self):
        return self._jobs_fn()

    @property
    def relocate(self) -> float:
        return self._config_fn().relocate

    @property
    def awaiting(self) -> float:
        return self._config_fn().awaiting

    @property
    def rescue(self) -> float:
        return self._config_fn().rescue

    def record_usage(self, pid: str, event, *, connection=None) -> bool:
        """Persist a usage report, returning whether it was new."""
        assert event.usage is not None
        u = event.usage
        participant = self.store.get_participant(pid)
        usage_key = u.idempotency_key
        if usage_key is not None and participant is not None:
            scope = participant.session_id or participant.transcript_location
            if scope:
                usage_key = f"{scope}:{usage_key}"
        return self.store.record_usage(
            participant_id=pid,
            tree_root_id=lineage.root_of(self.store, pid),
            usage_key=usage_key,
            ts=event.ts if event.ts is not None else self._wall_now_fn(),
            model=u.model,
            harness=participant.harness if participant is not None else "unknown",
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_creation_input_tokens=u.cache_creation_input_tokens,
            cache_read_input_tokens=u.cache_read_input_tokens,
            reasoning_output_tokens=u.reasoning_output_tokens,
            cost_microcents=usage_cost_microcents(u),
            connection=connection,
        )

    @staticmethod
    def is_write_free(batch: Batch) -> bool:
        """True when applying the batch can write nothing (terminal evidence needs the unit)."""
        return not (
            batch.progressed
            or batch.events
            or batch.attached is not None
            or batch.terminal_evidence
        )

    def apply(
        self,
        pid: str,
        batch: Batch,
        clock: QuietClock,
        turns: TurnAccumulator,
        *,
        answer_turn_fn,
        settle_fn,
        turn_result_fn,
        path_target_fn=None,
        connection=None,
    ) -> bool:
        """Put a batch on the bus and move the participant's status; return whether anything did.

        ``path_target_fn`` maps an event to its exact owning job (native live wiring); legacy
        wiring passes nothing and keeps the oldest-running heuristic.
        """
        if connection is None and self.is_write_free(batch):
            # Empty quiet poll: settle/floor/usage all no-op, so only telemetry remains.
            if self._telemetry_fn is not None:
                self._safe_telemetry(pid, batch, ())
            return False
        if connection is None:
            with (
                rollback_observation_state(clock, turns),
                self.staged_hashes(pid, batch),
                self.store.write_unit() as unit,
            ):
                return self.apply(
                    pid,
                    batch,
                    clock,
                    turns,
                    answer_turn_fn=answer_turn_fn,
                    settle_fn=settle_fn,
                    turn_result_fn=turn_result_fn,
                    path_target_fn=path_target_fn,
                    connection=unit.connection,
                )
        state = _ApplyState(usage_events=[] if self._telemetry_fn is not None else None)
        callbacks = _ApplyCallbacks(answer_turn_fn, turn_result_fn, path_target_fn)
        paths_now = any(event.turn_end for event in batch.events)
        context = _ApplyContext(
            pid, clock, turns, callbacks, state, connection, paths_now, batch.read_at
        )
        for event in batch.events:
            self._apply_event(event, context)
        self._settle_batch(
            pid,
            batch,
            state.last,
            lambda participant_id, status: settle_fn(participant_id, status, connection=connection),
        )
        self._clear_resolved_resume_floor(pid, batch, connection=connection)
        result = batch.progressed or bool(batch.events) or batch.attached is not None
        if self._telemetry_fn is not None:
            events = tuple(state.usage_events or ())
            unit = active_write_unit(connection)
            if unit is None:
                self._safe_telemetry(pid, batch, events)
            else:
                unit.after_commit(lambda: self._safe_telemetry(pid, batch, events))
        return result

    def _safe_telemetry(self, pid: str, batch: Batch, events: tuple) -> None:
        try:
            self._telemetry_fn(pid, batch, events)
        except Exception:
            logger.exception("agent telemetry failed for %s", pid)

    def _apply_event(self, event: Event, context: _ApplyContext) -> None:
        pid = context.pid
        state = context.state
        if (
            event.usage is not None
            and self.record_usage(pid, event, connection=context.connection)
            and state.usage_events is not None
        ):
            state.usage_events.append(event)
        if event.usage_only:
            return
        if state.observed_at is None:
            state.observed_at = self._wall_now_fn()
        payload = {
            "text": event.text,
            "tool": event.tool_name,
            "ts": event.ts,
            "turn_end": event.turn_end,
            "turn_terminal": event.turn_terminal,
            "turn": event.turn_id,
            "index": event.raw_index,
            "observed_at": state.observed_at,
        }
        if context.read_at is not None:
            payload["read_at"] = context.read_at
        self.store.bus_append(
            f"agent.{event.kind}",
            from_id=pid,
            payload=payload,
            connection=context.connection,
        )
        state.last = event
        self._observe_event_paths(event, context)
        if event.kind is EventKind.ASSISTANT and event.text:
            context.clock.last_text = event.text
            context.turns.say(event.text, raw_text=event.raw_text)
        if event.kind is EventKind.USER and event.text:
            context.turns.hear(event.text)
        if event.turn_end:
            turn = context.turns.take()
            if not context.turns.already_handled(event.turn_id):
                result_text, raw_result = context.callbacks.turn_result(event, turn)
                context.callbacks.answer_turn(
                    pid,
                    result_text,
                    turn.heard,
                    raw_result=raw_result,
                    terminal=event.turn_terminal,
                    connection=context.connection,
                )
                context.turns.mark_handled(event.turn_id)
            context.clock.last_text = ""
            # The finished job must not own the next turn's paths.
            state.job_handle = None

    def _observe_event_paths(self, event: Event, context: _ApplyContext) -> None:
        pid = context.pid
        state = context.state
        if not event.paths or self.jobs is None:
            return
        if context.callbacks.path_target is not None:
            target = context.callbacks.path_target(pid, event)
            if target:
                self._observe_paths(context, target, event.paths)
            return
        if state.job_handle is None:
            job = self.store.oldest_running_job_for_target(pid, connection=context.connection)
            state.job_handle = job.handle if job is not None else ""
        if state.job_handle:
            self._observe_paths(context, state.job_handle, event.paths)

    def staged_hashes(self, pid: str, batch: Batch) -> AbstractContextManager[None]:
        """Pre-hash a terminal batch's paths so its write unit performs no file I/O."""
        if self.jobs is None or not any(event.turn_end for event in batch.events):
            return nullcontext()
        paths = tuple(path for event in batch.events for path in event.paths or ())
        return self.jobs.stage_hashes(pid, paths)

    def _observe_paths(self, context: _ApplyContext, handle: str, paths) -> None:
        # Observation is idempotent (first-seen hash kept), so replay after rollback is safe.
        unit = active_write_unit(context.connection)
        if unit is None or context.paths_now:
            self.jobs.observe_paths(handle, paths)
        else:
            unit.after_commit(lambda: self.jobs.observe_paths(handle, paths))

    @staticmethod
    def _settle_batch(pid: str, batch: Batch, last: Event | None, settle_fn: Callable) -> None:
        # A no-progress status is a live restatement; it must not override screen status.
        if batch.status is not None and (batch.progressed or batch.events):
            settle_fn(pid, batch.status)
        elif last is not None:
            settle_fn(pid, status_after(last))

    def _clear_resolved_resume_floor(self, pid: str, batch: Batch, *, connection=None) -> None:
        if batch.attached is not None or not (batch.progressed or batch.events):
            return
        participant = self.store.get_participant(pid, connection=connection)
        if participant is not None and floor_is_present(participant.resume_floor):
            self.store.clear_resume_floor(pid, connection=connection)

    @staticmethod
    def has_semantic_progress(batch: Batch) -> bool:
        return (
            any(not event.usage_only for event in batch.events)
            or batch.status is not None
            or batch.attached is not None
        )

    def unblock_on_semantic_progress(self, pid: str, batch: Batch) -> None:
        """Preserve explicit source status; clear only stale display hints."""
        if batch.status is None and self.has_semantic_progress(batch):
            self._unblock(pid)

    async def on_progress(
        self, pid: str, observer: HarnessObserver, batch: Batch, clock: QuietClock
    ) -> None:
        """Reset only the clocks justified by this batch's evidence."""
        if self.has_semantic_progress(batch):
            clock.stir()
            return
        clock.stir_raw()
        await self._screen_status_due(pid, observer, clock)

    def settle(self, pid: str, desired: Status, *, connection=None) -> None:
        p = self.store.get_participant(pid, connection=connection)
        if p is None or p.status is Status.DEAD:
            return
        if p.status is desired:
            if self._wall_now_fn() - p.last_activity >= LAST_ACTIVITY_REFRESH_SECONDS:
                if connection is None:
                    self.registry.touch(pid)
                else:
                    self.store.touch(pid, connection=connection)
        elif connection is None:
            self.registry.set_status(pid, desired)
        else:
            self.store.set_status(pid, desired, connection=connection, publish=True)
            self.store.bus_append(
                "participant.status",
                to_id=pid,
                payload={"status": str(desired)},
                connection=connection,
            )

    def apply_screen_reading(self, pid: str, reading) -> None:
        # PROMPT -> IDLE cannot defer to rescue.
        if reading.kind in (ScreenKind.APPROVAL, ScreenKind.TRUST):
            self.settle(pid, Status.AWAITING_INPUT)
            logger.info("participant %s awaiting input (%s on screen)", pid, reading.kind)
        elif reading.kind is ScreenKind.WORKING:
            self.settle(pid, Status.WORKING)
        elif reading.kind is ScreenKind.PROMPT:
            self.settle(pid, Status.IDLE)

    async def check_idle_screen(self, pid: str, observer: HarnessObserver):
        """Map the rendered screen to a status, for any non-DEAD participant.

        The mapping is applied regardless of confidence. Being wrong here costs
        a mislabel in the display; the send gate requires high confidence.
        """
        p = self.store.get_participant(pid)
        if p is None or p.status is Status.DEAD:
            return None
        capture = await self._capture_fn(pid)
        if capture is None:
            return None
        reading = observer.screen_reading(capture)
        self.apply_screen_reading(pid, reading)
        return reading

    async def screen_is_positively_working(self, pid: str, observer: HarnessObserver) -> bool:
        from theater.harness import ScreenConfidence

        p = self.store.get_participant(pid)
        if p is None or p.status is Status.DEAD:
            return False
        capture = await self._capture_fn(pid)
        if capture is None:
            return False
        reading = observer.screen_reading(capture)
        self.apply_screen_reading(pid, reading)
        return reading.kind is ScreenKind.WORKING and reading.confidence is ScreenConfidence.HIGH

    async def on_quiet(
        self,
        pid: str,
        observer: HarnessObserver,
        source: Source,
        clock: QuietClock,
        turns: TurnAccumulator,
        *,
        source_status: Status | None = None,
        validate_batch_fn,
        report_source_error_fn,
        accept_attachment_fn,
        apply_fn,
        on_progress_fn,
        evidence_bound_fn,
        confirm_identity_loss_fn,
        mark_identity_lost_fn,
        reset_identity_loss_fn,
        is_untrusted_rotation_fn,
        rescue_jobs_fn,
    ) -> None:
        """Nothing arrived this tick. Run the three quiet timers.

        Ordering: relocate -> identity probe -> screen status -> rescue.
        None may reset another.
        """
        now = self._monotonic_fn()
        clock.begin_quiet(now)

        if clock.quiet_for(now) > self.relocate:
            batch = await source.refresh()
            validate_batch_fn(source, batch)
            report_source_error_fn(pid, batch)
            untrusted_refresh = batch.attached is not None and is_untrusted_rotation_fn(
                pid, batch.attached
            )
            if untrusted_refresh:
                source.discard_attachment()
            accepted = not untrusted_refresh and accept_attachment_fn(pid, source, batch)
            if accepted and apply_fn(pid, batch, clock, turns):
                await on_progress_fn(pid, observer, batch, clock)
                return
            evidence = await source.probe_identity_loss()
            if (
                evidence is not None
                and not evidence_bound_fn(pid, evidence)
                and await self.screen_is_positively_working(pid, observer)
            ):
                if confirm_identity_loss_fn(pid, evidence):
                    mark_identity_lost_fn(
                        pid,
                        (
                            "a newer same-harness/cwd transcript candidate appeared while the "
                            "trusted pin was inert and the pane was visibly working: "
                            f"{evidence.location}"
                        ),
                    )
                clock.quiet_since = now
                return
            reset_identity_loss_fn(pid)
            clock.quiet_since = now

        if clock.screen_quiet_for(now) > clock.screen_interval(self.awaiting):
            await self._screen_status_due(
                pid,
                observer,
                clock,
                source_status=source_status,
            )

        if clock.rescue_quiet_for(now) > self.rescue:
            oldest = None
            if self.jobs is not None:
                oldest = self.store.oldest_running_job_for_target(pid)
            if oldest is None:
                clock.rescue_since = now
            elif self._wall_now_fn() - oldest.created_at > self.rescue:
                await rescue_jobs_fn(pid, observer, clock)
                clock.rescue_since = now

    async def screen_only(
        self,
        pid: str,
        observer: HarnessObserver,
        clock: QuietClock,
        *,
        source_status: Status | None = None,
    ) -> None:
        """The screen arm of on_quiet, for a source that has not attached.

        One arm of the three, not all of them.
        """
        await self._screen_status_due(
            pid,
            observer,
            clock,
            source_status=source_status,
        )

    async def _screen_status_due(
        self,
        pid: str,
        observer: HarnessObserver,
        clock: QuietClock,
        *,
        source_status: Status | None = None,
    ) -> None:
        """Run the independently throttled status-only screen arm when due."""
        now = self._monotonic_fn()
        if clock.screen_quiet_since is None:
            clock.screen_quiet_since = now
        if clock.screen_quiet_for(now) > clock.screen_interval(self.awaiting):
            if source_status is Status.AWAITING_INPUT:
                return
            reading = await self.check_idle_screen(pid, observer)
            clock.note_screen(reading.kind if reading is not None else None)
            clock.screen_quiet_since = now

    def _unblock(self, pid: str) -> None:
        """New output means the agent is working, whatever the screen said."""
        p = self.store.get_participant(pid)
        if p and p.status is Status.AWAITING_INPUT:
            self.registry.set_status(pid, Status.WORKING)

    def turn_result(self, event, turn: Turn) -> tuple[str, str | object | None]:
        if not (event.text or event.raw_text):
            return turn.said, turn.raw_said
        if event.kind is EventKind.ERROR:
            return event.text, None
        return event.text, event.raw_text if event.raw_text is not None else event.text

    def settle_from_event(self, pid: str, event, *, answer_turn_fn, turn_result_fn) -> None:
        """Settle status and answer a turn from an attach-time event."""
        self.settle(pid, status_after(event))
        if event.turn_end:
            result_text, raw_result = turn_result_fn(event, Turn(""))
            answer_turn_fn(pid, result_text, raw_result=raw_result, terminal=event.turn_terminal)

    def end_turn_from_screen(self, pid: str, capture: str, *, answer_turn_fn) -> None:
        """Record a turn boundary that was seen rather than read."""
        text = end_turn_from_screen_text(capture)
        self.store.bus_append(
            "agent.assistant",
            from_id=pid,
            payload={
                "text": text,
                "tool": None,
                "ts": None,
                "turn_end": True,
                "index": -1,
                "source": "screen",
            },
        )
        answer_turn_fn(pid, text, raw_result=None)
