"""Central status policy: QuietClock, _apply, _on_quiet, _settle, screen status.

The reducer owns the three independent quiet timers, the screen-status
dispatch, the rescue decision, and the resume-floor suppression. ``_on_quiet``
ordering is preserved: relocate -> identity probe -> screen status -> rescue.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from theater.daemon import lineage
from theater.daemon.observation.screen import end_turn_from_screen_text
from theater.daemon.observation.turns import Turn, TurnAccumulator
from theater.harness import EventKind, HarnessObserver, ScreenKind, status_after
from theater.harness.source import Batch, Source
from theater.models import Status
from theater.pricing import usage_cost_microcents
from theater.resume_floor import floor_is_present

logger = logging.getLogger("theater.observer")


@dataclass
class QuietClock:
    """How long one participant's watcher has gone without hearing anything.

    Three quiet timers, not one. They measure the same silence but are reset by
    different events, and collapsing them into a single timer is a bug we have
    already shipped once. The rescue timer has the same problem in a worse form.
    """

    quiet_since: float | None = None
    screen_quiet_since: float | None = None
    rescue_since: float | None = None
    last_text: str = ""

    def stir(self) -> None:
        """Semantic output arrived: every timer starts again from zero."""
        self.quiet_since = None
        self.screen_quiet_since = None
        self.rescue_since = None

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
    ):
        self.store = store
        self.registry = registry
        self._wall_now_fn = wall_now_fn
        self._capture_fn = capture_fn
        self._monotonic_fn = monotonic_fn
        self._config_fn = config_fn
        self._jobs_fn = jobs_fn

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

    def record_usage(self, pid: str, event) -> bool:
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
        )

    def apply(  # noqa: PLR0912
        self,
        pid: str,
        batch: Batch,
        clock: QuietClock,
        turns: TurnAccumulator,
        *,
        answer_turn_fn,
        settle_fn,
        turn_result_fn,
    ) -> bool:
        """Put a batch on the bus and move the participant's status.

        Returns whether anything happened. Turn ends are answered inside the
        loop at every boundary. The turn's text lives in turns, which outlives
        this call.
        """
        job_handle: str | None = None
        last = None
        for event in batch.events:
            if event.usage is not None:
                self.record_usage(pid, event)
            if event.usage_only:
                continue
            self.store.bus_append(
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
                if self.jobs is not None and job_handle is None:
                    job = self.store.oldest_running_job_for_target(pid)
                    job_handle = job.handle if job is not None else ""
                if job_handle:
                    self.jobs.observe_paths(job_handle, event.paths)
            if event.kind is EventKind.ASSISTANT and event.text:
                clock.last_text = event.text
                turns.say(event.text, raw_text=event.raw_text)
            if event.kind is EventKind.USER and event.text:
                turns.hear(event.text)
            if event.turn_end:
                turn = turns.take()
                if not turns.already_handled(event.turn_id):
                    result_text, raw_result = turn_result_fn(event, turn)
                    answer_turn_fn(pid, result_text, turn.heard, raw_result=raw_result)
                    turns.mark_handled(event.turn_id)
                clock.last_text = ""
        if batch.status is not None:
            settle_fn(pid, batch.status)
        elif last is not None:
            settle_fn(pid, status_after(last))
        if batch.attached is None and (batch.progressed or batch.events):
            p_now = self.store.get_participant(pid)
            if p_now is not None and floor_is_present(p_now.resume_floor):
                self.store.clear_resume_floor(pid)
        return batch.progressed or bool(batch.events) or batch.attached is not None

    @staticmethod
    def has_semantic_progress(batch: Batch) -> bool:
        return (
            any(not event.usage_only for event in batch.events)
            or batch.status is not None
            or batch.attached is not None
        )

    def unblock_on_semantic_progress(self, pid: str, batch: Batch) -> None:
        if self.has_semantic_progress(batch):
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

    def settle(self, pid: str, desired: Status) -> None:
        p = self.store.get_participant(pid)
        if p is None or p.status is Status.DEAD:
            return
        if p.status is desired:
            self.registry.touch(pid)
        else:
            self.registry.set_status(pid, desired)

    def apply_screen_reading(self, pid: str, reading) -> None:
        # PROMPT -> IDLE cannot defer to rescue.
        if reading.kind in (ScreenKind.APPROVAL, ScreenKind.TRUST):
            self.settle(pid, Status.AWAITING_INPUT)
            logger.info("participant %s awaiting input (%s on screen)", pid, reading.kind)
        elif reading.kind is ScreenKind.WORKING:
            self.settle(pid, Status.WORKING)
        elif reading.kind is ScreenKind.PROMPT:
            self.settle(pid, Status.IDLE)

    async def check_idle_screen(self, pid: str, observer: HarnessObserver) -> None:
        """Map the rendered screen to a status, for any non-DEAD participant.

        The mapping is applied regardless of confidence. Being wrong here costs
        a mislabel in the display; the send gate requires high confidence.
        """
        p = self.store.get_participant(pid)
        if p is None or p.status is Status.DEAD:
            return
        if not p.tmux_pane:
            return
        capture = await self._capture_fn(p.tmux_pane)
        if capture is None:
            return
        reading = observer.screen_reading(capture)
        self.apply_screen_reading(pid, reading)

    async def screen_is_positively_working(self, pid: str, observer: HarnessObserver) -> bool:
        from theater.harness import ScreenConfidence

        p = self.store.get_participant(pid)
        if p is None or p.status is Status.DEAD or not p.tmux_pane:
            return False
        capture = await self._capture_fn(p.tmux_pane)
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

        if clock.screen_quiet_for(now) > self.awaiting:
            await self.check_idle_screen(pid, observer)
            clock.screen_quiet_since = now

        if clock.rescue_quiet_for(now) > self.rescue:
            oldest = None
            if self.jobs is not None:
                oldest = self.store.oldest_running_job_for_target(pid)
            if oldest is None:
                clock.rescue_since = now
            elif self._wall_now_fn() - oldest.created_at > self.rescue:
                await rescue_jobs_fn(pid, observer, clock)
                clock.rescue_since = now

    async def screen_only(self, pid: str, observer: HarnessObserver, clock: QuietClock) -> None:
        """The screen arm of on_quiet, for a source that has not attached.

        One arm of the three, not all of them.
        """
        await self._screen_status_due(pid, observer, clock)

    async def _screen_status_due(
        self, pid: str, observer: HarnessObserver, clock: QuietClock
    ) -> None:
        """Run the independently throttled status-only screen arm when due."""
        now = self._monotonic_fn()
        if clock.screen_quiet_since is None:
            clock.screen_quiet_since = now
        if clock.screen_quiet_for(now) > self.awaiting:
            await self.check_idle_screen(pid, observer)
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
            answer_turn_fn(pid, result_text, raw_result=raw_result)

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
