"""Observer lifecycle, supervision, and watch orchestration.

The ``Observer`` class owns the mutable state (watch tasks, source errors,
identity-loss sets, binding tables) and orchestrates the watch loop. It
delegates the status policy to ``reducer``, job completion to ``completion``,
attachment to ``attachment``, and source errors to ``failures``.

Constants that tests monkeypatch at call-time (``OBSERVATION_FAILURE_GRACE``,
``wall_now``, ``open_participant_source``) are read from the facade module
``theater.daemon.observer`` at call-time, not imported into this module's
globals, so patching ``theater.daemon.observer.X`` takes effect.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Sequence

from theater import timing
from theater.config import ObserverSection
from theater.constants.observation import (
    IDLE_CONFIRMATIONS,
    RAW_RESULT_UNSET,
    RESCUE_CODE,
    SOURCE_CONTRACT_FAILED,
)
from theater.daemon.observation.attachment import (
    accept_attachment as _accept_attachment_fn,
)
from theater.daemon.observation.attachment import (
    is_untrusted_rotation as _is_untrusted_rotation_fn,
)
from theater.daemon.observation.attachment import (
    on_attach as _on_attach_fn,
)
from theater.daemon.observation.attachment import (
    record_operator_binding as _record_operator_binding_fn,
)
from theater.daemon.observation.attachment import (
    release_transcript as _release_transcript_fn,
)
from theater.daemon.observation.attachment import (
    stage_receipt_source as _stage_receipt_source_fn,
)
from theater.daemon.observation.completion import (
    answer_turn as _answer_turn_fn,
)
from theater.daemon.observation.completion import (
    finish_identity_lost_jobs,
)
from theater.daemon.observation.completion import (
    release_jobs as _release_jobs_fn,
)
from theater.daemon.observation.failures import (
    clear_source_error_on_progress as _clear_error_on_progress_fn,
)
from theater.daemon.observation.failures import (
    clear_source_errors as _clear_source_errors_fn,
)
from theater.daemon.observation.failures import (
    confirm_identity_loss as _confirm_identity_loss_fn,
)
from theater.daemon.observation.failures import (
    handle_source_error as _handle_source_error_fn,
)
from theater.daemon.observation.failures import (
    reset_identity_loss_confirmation as _reset_id_loss_fn,
)
from theater.daemon.observation.failures import (
    restore_transcript_identity_loss as _restore_id_loss_fn,
)
from theater.daemon.observation.failures import (
    sweep_identity_lost_grace as _sweep_grace_fn,
)
from theater.daemon.observation.identity import (
    evidence_is_bound_to_another_live,
    has_cwd_competitor,
    history_correlation_is_ambiguous,
    location_bound_to_another_live,
    session_id_bound_to_another_live,
    trusted_dead_owner_blocks,
)
from theater.daemon.observation.reducer import (
    QuietClock,
    apply_batch,
    apply_screen_reading,
    has_semantic_progress,
    record_usage,
    settle,
    turn_result,
    unblock,
)
from theater.daemon.observation.screen import end_turn_from_screen_text
from theater.daemon.observation.turns import (
    Turn,
    TurnAccumulator,
)
from theater.daemon.registry import Registry
from theater.harness import (
    HARNESSES,
    Harness,
    HarnessObserver,
    ScreenConfidence,
    ScreenKind,
    status_after,
)
from theater.harness import (
    normalize as normalize_harness,
)
from theater.harness.source import (
    Attachment,
    Batch,
    History,
    IdentityLossEvidence,
    Source,
    SourceContractError,
)
from theater.models import JobState, Status, Tier
from theater.provenance import (
    normalize_provenance,
)
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    transcript_identity_recovery_message,
)

logger = logging.getLogger("theater.observer")

#: Fallback timings. ``config.ObserverSection`` owns the literal so the
#: default and the settable value cannot drift. Tests use these directly.
_DEFAULTS = ObserverSection()

POLL_INTERVAL = _DEFAULTS.poll_interval
RELOCATE_TIMEOUT = _DEFAULTS.relocate_timeout
AWAITING_INPUT_TIMEOUT = _DEFAULTS.awaiting_input_timeout
SEARCH_INTERVAL = _DEFAULTS.search_interval
SYNC_INTERVAL = _DEFAULTS.sync_interval
SCREEN_INTERVAL = _DEFAULTS.screen_interval
RESCUE_TIMEOUT = _DEFAULTS.rescue_timeout


class Observer:
    def __init__(
        self,
        registry: Registry,
        harnesses: dict[str, Harness] | None = None,
        *,
        poll: float = POLL_INTERVAL,
        search: float = SEARCH_INTERVAL,
        sync: float = SYNC_INTERVAL,
        relocate: float = RELOCATE_TIMEOUT,
        awaiting: float = AWAITING_INPUT_TIMEOUT,
        screen: float = SCREEN_INTERVAL,
        rescue: float = RESCUE_TIMEOUT,
        jobs=None,
    ):
        self.registry = registry
        self.store = registry.store
        #: Empty map is legitimate — "observe nothing" — which socket-level
        #: tests want, since real harness roots point at ~/.claude and ~/.vibe.
        self.harnesses = HARNESSES if harnesses is None else harnesses
        self.poll = poll
        self.search = search
        self.sync = sync
        self.relocate = relocate
        self.awaiting = awaiting
        self.screen = screen
        self.rescue = rescue
        #: When set, turn-end events for a participant with a running job
        #: finish that job with the assistant text as the result.
        self.jobs = jobs
        self._tasks: dict[str, asyncio.Task] = {}
        #: Participants whose watcher ended by itself. Not restarted.
        self._retired: set[str] = set()
        #: Participants we cannot observe, warned about once each.
        self._unobservable: set[str] = set()
        #: Per-job count of consecutive turn ends that did not match.
        self._unmatched: dict[str, int] = {}
        #: First wall-clock occurrence of each still-persistent source failure.
        self._source_errors: dict[tuple[str, str], float] = {}
        #: Transcript path -> participant id, for the live-binding guarantee.
        self._bound_transcripts: dict[str, str] = {}
        self._binding_correlation: dict[str, str] = {}
        self._binding_sessions: dict[str, str | None] = {}
        self._sources: dict[str, Source] = {}
        self._receipt_candidates: dict[str, tuple[str, str]] = {}
        self._reset_watch_state: set[str] = set()
        #: Live quarantine state.
        self._identity_lost: set[str] = set()
        self._identity_loss_replayed: set[str] = set()
        #: Pending identity-loss confirmations: pid -> (location, count).
        self._identity_loss_pending: dict[str, tuple[str, int]] = {}
        self._supervisor: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    # ---- call-time hooks for monkeypatched globals ---------------------

    @staticmethod
    def _wall_now() -> float:
        """Read ``wall_now`` from the facade at call-time for monkeypatch support."""
        from theater.daemon import observer as _facade

        return _facade.wall_now()

    @staticmethod
    def _open_participant_source(*args, **kwargs):
        """Read ``open_participant_source`` from the facade at call-time."""
        from theater.daemon import observer as _facade

        return _facade.open_participant_source(*args, **kwargs)

    @staticmethod
    def _grace() -> float:
        """Read ``OBSERVATION_FAILURE_GRACE`` from the facade at call-time."""
        from theater.daemon import observer as _facade

        return _facade.OBSERVATION_FAILURE_GRACE

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if not self.harnesses:
            logger.debug("no harnesses configured; observation disabled")
            return
        # Seed watcher-local quarantine caches before the socket can service a
        # send against a participant whose restart audit has not been replayed.
        self._reconcile()
        self._supervisor = asyncio.create_task(self._supervise())

    async def aclose(self) -> None:
        self._stopping.set()
        tasks = list(self._tasks.values())
        if self._supervisor:
            tasks.append(self._supervisor)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._supervisor = None

    async def reset_for_operator_bind(self, pid: str) -> None:
        """Stop a live watcher so it reopens from the operator-pinned location."""
        task = self._tasks.pop(pid, None)
        self._retired.discard(pid)
        self._reset_watch_state.discard(pid)
        self._clear_source_errors(pid, include_identity_lost=True)
        self._identity_loss_replayed.discard(pid)
        if task is not None:
            task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task

    def record_operator_binding(
        self,
        pid: str,
        location: str,
        session_id: str | None,
        *,
        prior_owner: str | None = None,
    ) -> None:
        """Mirror an accepted operator binding in the live collision table."""
        _record_operator_binding_fn(
            pid,
            location,
            session_id,
            prior_owner=prior_owner,
            store=self.store,
            bound_transcripts=self._bound_transcripts,
            binding_correlation=self._binding_correlation,
            binding_sessions=self._binding_sessions,
            release_transcript_fn=self._release_transcript,
            clear_source_errors_fn=self._clear_source_errors,
        )

    def transcript_identity_lost(self, pid: str) -> bool:
        """Pure cached predicate; only the watch path may enter quarantine."""
        participant = self.store.get_participant(pid)
        if participant is None or participant.status is Status.DEAD:
            return False
        return pid in self._identity_lost

    def _restore_transcript_identity_loss(self, pid: str) -> None:
        """Replay retained audit once for this watcher lifecycle."""
        _restore_id_loss_fn(
            pid,
            store=self.store,
            jobs=self.jobs,
            identity_lost=self._identity_lost,
            identity_loss_replayed=self._identity_loss_replayed,
            source_errors=self._source_errors,
            finish_fn=self._finish,
            wall_now_fn=self._wall_now,
            grace=self._grace(),
        )

    def _sweep_identity_lost_grace(self, pid: str, failed_at: float | None = None) -> None:
        """Re-evaluate running jobs against the identity-loss grace window."""
        _sweep_grace_fn(
            pid,
            failed_at,
            store=self.store,
            jobs=self.jobs,
            finish_fn=self._finish,
            source_errors=self._source_errors,
            wall_now_fn=self._wall_now,
            grace=self._grace(),
        )

    def _evidence_is_bound_to_another_live_participant(
        self, pid: str, evidence: IdentityLossEvidence
    ) -> bool:
        """Whether loss evidence names a transcript another live participant owns."""
        return evidence_is_bound_to_another_live(
            pid,
            evidence.location,
            evidence.session_id,
            self._bound_transcripts,
            self._binding_sessions,
            self.store,
            self.registry,
        )

    def _location_bound_to_another_live(self, pid: str, location: str) -> bool:
        """Whether *location* is claimed by a different live participant."""
        return location_bound_to_another_live(
            pid, location, self._bound_transcripts, self.store, self.registry
        )

    def _session_id_bound_to_another_live(self, pid: str, session_id: str | None) -> bool:
        """Whether *session_id* is claimed by a different live participant."""
        return session_id_bound_to_another_live(
            pid,
            session_id,
            self._bound_transcripts,
            self._binding_sessions,
            self.store,
            self.registry,
        )

    def _confirm_identity_loss(self, pid: str, evidence: IdentityLossEvidence) -> bool:
        """Require consecutive relocate windows with the same evidence location."""
        return _confirm_identity_loss_fn(
            pid, evidence, identity_loss_pending=self._identity_loss_pending
        )

    def _reset_identity_loss_confirmation(self, pid: str) -> None:
        """Semantic progress on the pinned source resets confirmation."""
        _reset_id_loss_fn(pid, identity_loss_pending=self._identity_loss_pending)

    def mark_transcript_identity_lost(self, pid: str, reason: str) -> None:
        """Enter quarantine from positive evidence in the observation path."""
        participant = self.store.get_participant(pid)
        if participant is None or participant.status is Status.DEAD:
            return
        self._handle_source_error(
            pid,
            Batch(
                waiting=True,
                error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
                error=transcript_identity_recovery_message(pid, reason),
            ),
        )

    async def _sleep(self, seconds: float) -> None:
        """Wait, but wake immediately on shutdown."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    # ---- supervision ---------------------------------------------------

    async def _supervise(self) -> None:
        while not self._stopping.is_set():
            try:
                self._reconcile()
            except Exception:
                logger.exception("observer reconcile failed")
            await self._sleep(self.sync)

    def _reconcile(self) -> None:
        live = {p.id: p for p in self.registry.list()}
        for pid, task in list(self._tasks.items()):
            if pid in live and not task.done():
                continue
            self._tasks.pop(pid)
            task.cancel()
            if pid in live:
                self._retired.add(pid)
                logger.warning("observer for %s stopped; not restarting", pid)
        for pid, p in live.items():
            if pid in self._tasks or pid in self._retired:
                continue
            harness = self.harnesses.get(normalize_harness(p.harness))
            if harness is None:
                self._warn_unobservable(pid, p)
                continue
            observer = harness.observer
            # A transcript is found by cwd; a screen by pane. So cwd is
            # required for one loop and irrelevant to the other.
            if observer.has_transcript and not p.cwd:
                self._warn_unobservable(pid, p)
                continue
            if p.tier is Tier.SPAWNED and p.tmux_pane is None:
                continue
            self._unobservable.discard(pid)
            watch = self._watch if observer.has_transcript else self._watch_screen
            if observer.has_transcript:
                self._restore_transcript_identity_loss(pid)
            timing.ready_lag("observer.watch", pid, p.created_at, harness=p.harness)
            self._tasks[pid] = asyncio.create_task(watch(pid, normalize_harness(p.harness)))

    def _warn_unobservable(self, pid: str, p) -> None:
        if pid in self._unobservable:
            return
        self._unobservable.add(pid)
        if normalize_harness(p.harness) not in self.harnesses:
            known = ", ".join(sorted(self.harnesses)) or "none"
            reason = f"harness {p.harness!r} is not one we can read (known: {known})"
        else:
            reason = "it reported no working directory"
        logger.warning("cannot observe %s: %s", pid, reason)

    # ---- one participant -----------------------------------------------

    async def _watch(self, pid: str, harness_name: str) -> None:  # noqa: PLR0912, PLR0915
        observer = self.harnesses[harness_name].observer
        source = self._open_source(pid, observer)
        if source is None:
            return
        self._restore_transcript_identity_loss(pid)
        clock = QuietClock()
        turns = TurnAccumulator()

        try:
            try:
                self._register_source(pid, source)
            except SourceContractError:
                logger.exception(SOURCE_CONTRACT_FAILED, pid)
                return
            while not self._stopping.is_set():
                try:
                    if pid in self._reset_watch_state:
                        self._reset_watch_state.discard(pid)
                        clock = QuietClock()
                        turns = TurnAccumulator()
                    if self.transcript_identity_lost(pid):
                        # Sweep before screen so a broken third-party screen
                        # classifier cannot starve job terminalization.
                        self._sweep_identity_lost_grace(pid)
                        await self._screen_only(pid, observer, clock)
                        await self._sleep(self.search)
                        continue
                    batch = await source.read()
                    self._validate_batch(source, batch)
                    if batch.waiting:
                        self._update_source_error(pid, batch)
                        await self._screen_only(pid, observer, clock)
                        await self._sleep(self.search)
                        continue
                    self._report_source_error(pid, batch)
                    if not self._accept_attachment(pid, source, batch):
                        await self._screen_only(pid, observer, clock)
                        await self._sleep(self.search)
                        continue
                    self._clear_source_error_on_progress(pid, batch)
                    if self._apply(pid, batch, clock, turns):
                        self._unblock_on_semantic_progress(pid, batch)
                        await self._on_progress(pid, observer, batch, clock)
                    else:
                        await self._on_quiet(pid, observer, source, clock, turns)
                except asyncio.CancelledError:
                    raise
                except SourceContractError:
                    logger.exception(SOURCE_CONTRACT_FAILED, pid)
                    return
                except Exception:
                    logger.exception("observing %s failed", pid)
                await self._sleep(self.poll)
        finally:
            self._clear_source_errors(pid, include_identity_lost=True)
            self._identity_loss_replayed.discard(pid)
            self._reset_watch_state.discard(pid)
            self._receipt_candidates.pop(pid, None)
            self._sources.pop(pid, None)
            self._release_transcript(pid)
            try:
                await source.aclose()
            except (Exception, asyncio.CancelledError):
                logger.debug("closing source for %s failed", pid, exc_info=True)

    def _open_source(self, pid: str, observer: HarnessObserver) -> Source | None:
        """Build the source for a participant, from what the registry knows."""
        p = self.store.get_participant(pid)
        if p is None:
            return None
        after = p.created_at if p.tier is Tier.SPAWNED else None
        source = self._open_participant_source(
            observer,
            participant_id=p.id,
            cwd=p.cwd,
            session_id=p.session_id,
            after=after,
            session_provenance=normalize_provenance(p.session_correlation),
            known_location=p.transcript_location,
            transcript_domain=p.transcript_domain,
            pane_pid=p.live_pid,
        )
        if source.collision_domain is not None and p.transcript_domain != source.collision_domain:
            p.transcript_domain = source.collision_domain
            self.store.upsert_participant(p)
        return source

    def _register_source(self, pid: str, source: Source) -> None:
        self._sources[pid] = source
        self._stage_pending_receipt(pid, source)

    @staticmethod
    def _validate_batch(source: Source, batch: Batch) -> None:
        """Reject contradictory source facts without stranding a candidate."""
        if not (batch.waiting and batch.attached is not None):
            return
        source.discard_attachment()
        raise SourceContractError(
            f"{type(source).__name__} returned a batch that is both waiting and attached"
        )

    def _record_usage(self, pid: str, event) -> bool:
        """Persist a usage report, returning whether it was new."""
        return record_usage(pid, event, store=self.store, jobs=self.jobs)

    def _apply(self, pid: str, batch: Batch, clock: QuietClock, turns: TurnAccumulator) -> bool:
        """Put a batch on the bus and move the participant's status."""
        return apply_batch(
            pid,
            batch,
            clock,
            turns,
            store=self.store,
            registry=self.registry,
            jobs=self.jobs,
            record_usage_fn=self._record_usage,
            settle_fn=self._settle,
            answer_turn_fn=self._answer_turn,
            turn_result_fn=turn_result,
        )

    @staticmethod
    def _has_semantic_progress(batch: Batch) -> bool:
        """Whether a batch says something about the participant, not just its source."""
        return has_semantic_progress(batch)

    def _unblock_on_semantic_progress(self, pid: str, batch: Batch) -> None:
        """Raw bookkeeping cannot overrule a modal found by the screen arm."""
        if self._has_semantic_progress(batch):
            self._unblock(pid)

    async def _on_progress(
        self,
        pid: str,
        observer: HarnessObserver,
        batch: Batch,
        clock: QuietClock,
    ) -> None:
        """Reset only the clocks justified by this batch's evidence."""
        if self._has_semantic_progress(batch):
            clock.stir()
            return
        clock.stir_raw()
        await self._screen_status_due(pid, observer, clock)

    def _handle_source_error(self, pid: str, batch: Batch) -> None:
        """Report broken exact correlation and bound affected awaits."""
        _handle_source_error_fn(
            pid,
            batch,
            store=self.store,
            jobs=self.jobs,
            source_errors=self._source_errors,
            identity_lost=self._identity_lost,
            bus_append_fn=self.store.bus_append,
            finish_fn=self._finish,
            wall_now_fn=self._wall_now,
            grace=self._grace(),
        )

    def _update_source_error(self, pid: str, batch: Batch) -> None:
        if batch.error_code is None:
            self._clear_source_errors(pid)
            return
        self._handle_source_error(pid, batch)

    def _report_source_error(self, pid: str, batch: Batch) -> None:
        if batch.error_code is not None:
            self._handle_source_error(pid, batch)

    def _clear_source_error_on_progress(self, pid: str, batch: Batch) -> None:
        _clear_error_on_progress_fn(
            pid,
            batch,
            source_errors=self._source_errors,
            identity_lost=self._identity_lost,
            identity_loss_pending=self._identity_loss_pending,
            reset_identity_loss_confirmation_fn=self._reset_identity_loss_confirmation,
        )

    def _clear_source_errors(self, pid: str, *, include_identity_lost: bool = False) -> None:
        _clear_source_errors_fn(
            pid,
            source_errors=self._source_errors,
            identity_lost=self._identity_lost,
            identity_loss_pending=self._identity_loss_pending,
            include_identity_lost=include_identity_lost,
        )

    def _turn_result(self, event, turn: Turn) -> tuple[str, str | object | None]:
        return turn_result(event, turn)

    async def _watch_screen(self, pid: str, harness_name: str) -> None:
        """Derive status from the rendered screen, for a parser-less harness."""
        observer = self.harnesses[harness_name].observer
        idle_streak = 0
        ended = False

        while not self._stopping.is_set():
            try:
                p = self.store.get_participant(pid)
                if p is None or p.status is Status.DEAD:
                    return
                capture = await self._capture(p.tmux_pane) if p.tmux_pane else None
                if capture is not None:
                    idle_streak = idle_streak + 1 if observer.is_idle_screen(capture) else 0
                    if idle_streak >= IDLE_CONFIRMATIONS:
                        if not ended:
                            ended = True
                            self._end_turn_from_screen(pid, capture)
                        self._settle(pid, Status.IDLE)
                    elif idle_streak == 0:
                        ended = False
                        self._settle(pid, Status.WORKING)
                    # A streak of one is undecided.
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("observing screen of %s failed", pid)
            await self._sleep(self.screen)

    def _end_turn_from_screen(self, pid: str, capture: str) -> None:
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
        self._answer_turn(pid, text, raw_result=None)

    async def _capture(self, pane: str) -> str | None:
        """The pane's rendered text, or None if it could not be read."""
        from theater.tmux import client as tmux

        try:
            return await tmux.run("capture-pane", "-p", "-t", pane, check=False)
        except Exception:
            return None

    def _unblock(self, pid: str) -> None:
        """New output means the agent is working, whatever the screen said."""
        unblock(pid, store=self.store, registry=self.registry)

    async def _on_quiet(
        self,
        pid: str,
        observer: HarnessObserver,
        source: Source,
        clock: QuietClock,
        turns: TurnAccumulator,
    ) -> None:
        """Nothing arrived this tick. Run the three quiet timers.

        All read the same silence, and none may reset another: see
        QuietClock for what happens when they share a clock.
        """
        now = time.monotonic()
        clock.begin_quiet(now)

        # Ask the source whether it should be reading somewhere else.
        if clock.quiet_for(now) > self.relocate:
            batch = await source.refresh()
            self._validate_batch(source, batch)
            self._report_source_error(pid, batch)
            untrusted_refresh = batch.attached is not None and self._is_untrusted_rotation(
                pid, batch.attached
            )
            if untrusted_refresh:
                source.discard_attachment()
            accepted = not untrusted_refresh and self._accept_attachment(pid, source, batch)
            if accepted and self._apply(pid, batch, clock, turns):
                await self._on_progress(pid, observer, batch, clock)
                return
            evidence = await source.probe_identity_loss()
            if (
                evidence is not None
                and not self._evidence_is_bound_to_another_live_participant(pid, evidence)
                and await self._screen_is_positively_working(pid, observer)
            ):
                if self._confirm_identity_loss(pid, evidence):
                    self.mark_transcript_identity_lost(
                        pid,
                        (
                            "a newer same-harness/cwd transcript candidate appeared while the "
                            "trusted pin was inert and the pane was visibly working: "
                            f"{evidence.location}"
                        ),
                    )
                clock.quiet_since = now
                return
            # A relocate window that found no admissible evidence breaks the
            # consecutive chain. Reset so the next qualifying window starts fresh.
            self._reset_identity_loss_confirmation(pid)
            clock.quiet_since = now

        if clock.screen_quiet_for(now) > self.awaiting:
            await self._check_idle_screen(pid, observer)
            clock.screen_quiet_since = now  # throttle

        if clock.rescue_quiet_for(now) > self.rescue:
            oldest = None
            if self.jobs is not None:
                oldest = self.store.oldest_running_job_for_target(pid)
            if oldest is None:
                clock.rescue_since = now  # throttle
            elif self._wall_now() - oldest.created_at > self.rescue:
                await self._rescue_jobs(pid, observer, clock)
                clock.rescue_since = now  # throttle

    async def _screen_only(self, pid: str, observer: HarnessObserver, clock: QuietClock) -> None:
        """The screen arm of ``_on_quiet``, for a source that has not attached.

        One arm of the three, not all of them, and that is the whole point of
        keeping it separate rather than calling ``_on_quiet`` here.
        """
        await self._screen_status_due(pid, observer, clock)

    async def _screen_status_due(
        self, pid: str, observer: HarnessObserver, clock: QuietClock
    ) -> None:
        """Run the independently throttled status-only screen arm when due."""
        now = time.monotonic()
        if clock.screen_quiet_since is None:
            clock.screen_quiet_since = now
        if clock.screen_quiet_for(now) > self.awaiting:
            await self._check_idle_screen(pid, observer)
            clock.screen_quiet_since = now  # throttle

    def _is_untrusted_rotation(self, pid: str, attached: Attachment) -> bool:
        return _is_untrusted_rotation_fn(pid, attached, self.store)

    async def _screen_is_positively_working(self, pid: str, observer: HarnessObserver) -> bool:
        p = self.store.get_participant(pid)
        if p is None or p.status is Status.DEAD or not p.tmux_pane:
            return False
        capture = await self._capture(p.tmux_pane)
        if capture is None:
            return False
        reading = observer.screen_reading(capture)
        self._apply_screen_reading(pid, reading)
        return reading.kind is ScreenKind.WORKING and reading.confidence is ScreenConfidence.HIGH

    def _accept_attachment(self, pid: str, source: Source, batch: Batch) -> bool:
        return _accept_attachment_fn(
            pid,
            source,
            batch,
            store=self.store,
            registry=self.registry,
            sources=self._sources,
            bound_transcripts=self._bound_transcripts,
            binding_correlation=self._binding_correlation,
            binding_sessions=self._binding_sessions,
            handle_source_error_fn=self._handle_source_error,
            on_attach_fn=self._on_attach,
            clear_source_errors_fn=self._clear_source_errors,
        )

    def _has_cwd_competitor(self, pid: str, collision_domain: str | None) -> bool:
        return has_cwd_competitor(pid, collision_domain, self.store, self.registry, self._sources)

    def _trusted_dead_owner_blocks(self, pid: str, attached: Attachment) -> bool:
        return trusted_dead_owner_blocks(pid, attached, self.store, self.registry)

    def history_is_ambiguous(self, pid: str, history: History) -> bool:
        """Whether a short-lived history read is only a contested cwd guess."""
        return history_correlation_is_ambiguous(self.registry, pid, history)

    def transcript_receipt(self, pid: str, *, location: str, session_id: str) -> str:
        """Stage exact receipt evidence without persisting it before admission."""
        source = self._sources.get(pid)
        if source is None:
            self._receipt_candidates[pid] = (location, session_id)
            return "staged"
        return self._stage_receipt_source(pid, source, location=location, session_id=session_id)

    def _stage_pending_receipt(self, pid: str, source: Source) -> None:
        candidate = self._receipt_candidates.pop(pid, None)
        if candidate is None:
            return
        location, session_id = candidate
        self._stage_receipt_source(pid, source, location=location, session_id=session_id)

    def _stage_receipt_source(
        self, pid: str, source: Source, *, location: str, session_id: str
    ) -> str:
        return _stage_receipt_source_fn(
            pid,
            source,
            location=location,
            session_id=session_id,
            store=self.store,
            binding_correlation=self._binding_correlation,
            binding_sessions=self._binding_sessions,
            clear_source_errors_fn=self._clear_source_errors,
        )

    def _on_attach(self, pid: str, attached: Attachment) -> None:
        _on_attach_fn(
            pid,
            attached,
            store=self.store,
            registry=self.registry,
            bound_transcripts=self._bound_transcripts,
            binding_correlation=self._binding_correlation,
            binding_sessions=self._binding_sessions,
            release_transcript_fn=self._release_transcript,
            settle_fn=self._settle,
            settle_from_event_fn=self._settle_from_event,
            answer_turn_fn=self._answer_turn,
            turn_result_fn=turn_result,
            timing_fn=timing.ready_lag,
        )

    def _settle_from_event(self, pid: str, event) -> None:
        """Settle status and answer a turn from an attach-time event."""
        self._settle(pid, status_after(event))
        if event.turn_end:
            result_text, raw_result = self._turn_result(event, Turn(""))
            self._answer_turn(pid, result_text, raw_result=raw_result)

    def _release_transcript(self, pid: str) -> None:
        """Drop a participant's claim on its transcript, if it still holds it."""
        _release_transcript_fn(
            pid,
            bound_transcripts=self._bound_transcripts,
            binding_correlation=self._binding_correlation,
            binding_sessions=self._binding_sessions,
        )

    def _settle(self, pid: str, desired: Status) -> None:
        settle(pid, desired, store=self.store, registry=self.registry)

    async def _check_idle_screen(self, pid: str, observer: HarnessObserver) -> None:
        """Map the rendered screen to a status, for any non-DEAD participant.

        The mapping is applied regardless of confidence. Being wrong here costs
        a mislabel in the display; the send gate, which is built on the same
        ``screen_reading``, requires ``high`` confidence because being wrong
        there makes a pane permanently unreachable. The asymmetry is deliberate.
        """
        p = self.store.get_participant(pid)
        if p is None or p.status is Status.DEAD:
            return
        if not p.tmux_pane:
            return
        capture = await self._capture(p.tmux_pane)
        if capture is None:
            return
        reading = observer.screen_reading(capture)
        self._apply_screen_reading(pid, reading)

    def _apply_screen_reading(self, pid: str, reading) -> None:
        apply_screen_reading(pid, reading, store=self.store, registry=self.registry)

    async def _rescue_jobs(self, pid: str, observer: HarnessObserver, clock: QuietClock) -> None:
        """Finish a job whose turn end was never read, so the caller unblocks."""
        if self.jobs is None or not self.store.running_jobs_for_target(pid):
            return
        p = self.store.get_participant(pid)
        if p is None or not p.tmux_pane:
            return
        capture = await self._capture(p.tmux_pane)
        if capture is None:
            return
        if observer.screen_reading(capture).kind is not ScreenKind.PROMPT:
            return
        logger.warning(
            "no turn end seen for %s after %.0fs of quiet; finishing its jobs",
            pid,
            self.rescue,
        )
        self._release_jobs(
            pid,
            clock.last_text,
            error_code=RESCUE_CODE,
            raw_result=None,
        )

    def _answer_turn(
        self,
        pid: str,
        result_text: str,
        heard: Sequence[str] = (),
        *,
        raw_result: str | object | None = RAW_RESULT_UNSET,
    ) -> None:
        _answer_turn_fn(
            pid,
            result_text,
            heard,
            store=self.store,
            jobs=self.jobs,
            unmatched=self._unmatched,
            raw_result=raw_result,
            finish_fn=self._finish,
        )

    def _release_jobs(
        self,
        pid: str,
        result_text: str,
        *,
        error_code: str | None = None,
        raw_result: str | object | None = RAW_RESULT_UNSET,
    ) -> None:
        _release_jobs_fn(
            pid,
            result_text,
            error_code=error_code,
            raw_result=raw_result,
            store=self.store,
            jobs=self.jobs,
            finish_fn=self._finish,
        )

    def _finish_identity_lost_jobs(self, pid: str, result_text: str) -> None:
        finish_identity_lost_jobs(
            pid,
            result_text,
            store=self.store,
            jobs=self.jobs,
            finish_fn=self._finish,
        )

    def _finish(
        self,
        handle: str,
        result_text: str,
        *,
        error_code: str | None = None,
        state: JobState = JobState.DONE,
        raw_result: str | object | None = RAW_RESULT_UNSET,
    ) -> None:
        """Resolve one job. The result is already clipped by the parser."""
        assert self.jobs is not None
        self._unmatched.pop(handle, None)
        if raw_result is RAW_RESULT_UNSET:
            self.jobs.finish(
                handle,
                state=state,
                result=result_text or "",
                error_code=error_code,
            )
        else:
            self.jobs.finish(
                handle,
                state=state,
                result=result_text or "",
                error_code=error_code,
                raw_result=raw_result,
            )
