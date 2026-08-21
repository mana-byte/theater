"""Observer lifecycle, supervision, and watch orchestration.

The ``Observer`` class owns the mutable watch-task set and orchestrates the
watch loop. It delegates status policy to ``Reducer``, job completion to
``CompletionTracker``, attachment to ``AttachmentManager``, and source errors
to ``FailureTracker``. All four are concrete, explicitly wired collaborators.

Constants that tests monkeypatch at call-time are read from the facade module
``theater.daemon.observer`` at call-time via ``_wall_now``/``_grace`` hooks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence

from theater import timing
from theater.config import ObserverSection
from theater.constants.observation import (
    RAW_RESULT_UNSET,
    SOURCE_CONTRACT_FAILED,
)
from theater.daemon.observation.attachment import AttachmentManager
from theater.daemon.observation.completion import CompletionTracker
from theater.daemon.observation.failures import FailureTracker
from theater.daemon.observation.identity import history_correlation_is_ambiguous
from theater.daemon.observation.reducer import QuietClock, Reducer
from theater.daemon.observation.turns import Turn, TurnAccumulator
from theater.daemon.registry import Registry
from theater.harness import (
    HARNESSES,
    Event,
    Harness,
    HarnessObserver,
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
from theater.provenance import normalize_provenance

logger = logging.getLogger("theater.observer")

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
        self.harnesses = HARNESSES if harnesses is None else harnesses
        self.poll = poll
        self.search = search
        self.sync = sync
        self.relocate = relocate
        self.awaiting = awaiting
        self.screen = screen
        self.rescue = rescue
        self.jobs = jobs
        self._tasks: dict[str, asyncio.Task] = {}
        self._retired: set[str] = set()
        self._unobservable: set[str] = set()
        self._supervisor: asyncio.Task | None = None
        self._stopping = asyncio.Event()

        # Concrete collaborators, explicitly wired.
        self._completion = CompletionTracker(self.store, self.registry, jobs_fn=lambda: self.jobs)
        self._failures = FailureTracker(
            self.store,
            self.registry,
            wall_now_fn=self._wall_now,
            grace_fn=self._grace,
            jobs_fn=lambda: self.jobs,
        )
        self._attachments = AttachmentManager(
            self.store,
            self.registry,
            timing_fn=lambda *a, **kw: timing.ready_lag(*a, **kw),  # noqa: PLW0108
        )
        self._reducer = Reducer(
            self.store,
            self.registry,
            wall_now_fn=self._wall_now,
            capture_fn=self._capture_for_reducer,
            monotonic_fn=self._monotonic,
            config_fn=lambda: self,
            jobs_fn=lambda: self.jobs,
        )

    # ---- call-time hooks for monkeypatched globals ---------------------

    @staticmethod
    def _wall_now() -> float:
        from theater.daemon import observer as _facade

        return _facade.wall_now()

    @staticmethod
    def _open_participant_source(*args, **kwargs):
        from theater.daemon import observer as _facade

        return _facade.open_participant_source(*args, **kwargs)

    @staticmethod
    def _grace() -> float:
        from theater.daemon import observer as _facade

        return _facade.OBSERVATION_FAILURE_GRACE

    @staticmethod
    def _monotonic() -> float:
        """Read time.monotonic from the facade at call-time for monkeypatch support."""
        from theater.daemon import observer as _facade

        return _facade.time.monotonic()

    def _finish(
        self,
        handle: str,
        result_text: str,
        *,
        error_code: str | None = None,
        state: JobState = JobState.DONE,
        raw_result: str | object | None = RAW_RESULT_UNSET,
    ) -> None:
        self._completion._finish(
            handle, result_text, error_code=error_code, state=state, raw_result=raw_result
        )

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if not self.harnesses:
            logger.debug("no harnesses configured; observation disabled")
            return
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
        task = self._tasks.pop(pid, None)
        self._retired.discard(pid)
        self._attachments._reset_watch_state.discard(pid)
        self._failures.clear_source_errors(pid, include_identity_lost=True)
        self._failures._identity_loss_replayed.discard(pid)
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
        self._attachments.record_operator_binding(
            pid,
            location,
            session_id,
            prior_owner=prior_owner,
            clear_source_errors_fn=self._failures.clear_source_errors,
        )

    def transcript_identity_lost(self, pid: str) -> bool:
        return self._failures.transcript_identity_lost(pid)

    def _restore_transcript_identity_loss(self, pid: str) -> None:
        self._failures.restore_transcript_identity_loss(pid, finish_fn=self._finish)

    def _sweep_identity_lost_grace(self, pid: str, failed_at: float | None = None) -> None:
        self._failures.sweep_identity_lost_grace(pid, failed_at, finish_fn=self._finish)

    def mark_transcript_identity_lost(self, pid: str, reason: str) -> None:
        self._failures.mark_transcript_identity_lost(pid, reason, finish_fn=self._finish)

    async def _sleep(self, seconds: float) -> None:
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
                    if pid in self._attachments._reset_watch_state:
                        self._attachments._reset_watch_state.discard(pid)
                        clock = QuietClock()
                        turns = TurnAccumulator()
                    if self.transcript_identity_lost(pid):
                        self._sweep_identity_lost_grace(pid)
                        await self._screen_only(pid, observer, clock)
                        await self._sleep(self.search)
                        continue
                    batch = await source.read()
                    self._validate_batch(source, batch)
                    if batch.waiting:
                        self._failures.update_source_error(pid, batch, finish_fn=self._finish)
                        await self._screen_only(pid, observer, clock)
                        await self._sleep(self.search)
                        continue
                    self._failures.report_source_error(pid, batch, finish_fn=self._finish)
                    if not self._accept_attachment(pid, source, batch):
                        await self._screen_only(pid, observer, clock)
                        await self._sleep(self.search)
                        continue
                    self._failures.clear_source_error_on_progress(pid, batch)
                    if self._reducer.apply(
                        pid,
                        batch,
                        clock,
                        turns,
                        answer_turn_fn=self._answer_turn,
                        settle_fn=self._settle,
                        turn_result_fn=self._turn_result,
                    ):
                        self._reducer.unblock_on_semantic_progress(pid, batch)
                        await self._reducer.on_progress(pid, observer, batch, clock)
                    else:
                        await self._reducer.on_quiet(
                            pid,
                            observer,
                            source,
                            clock,
                            turns,
                            validate_batch_fn=self._validate_batch,
                            report_source_error_fn=lambda p, b: self._failures.report_source_error(
                                p, b, finish_fn=self._finish
                            ),
                            accept_attachment_fn=self._accept_attachment,
                            apply_fn=lambda p, b, c, t: self._reducer.apply(
                                p,
                                b,
                                c,
                                t,
                                answer_turn_fn=self._answer_turn,
                                settle_fn=self._settle,
                                turn_result_fn=self._turn_result,
                            ),
                            on_progress_fn=self._reducer.on_progress,
                            evidence_bound_fn=self._evidence_is_bound_to_another_live_participant,
                            confirm_identity_loss_fn=self._confirm_identity_loss,
                            mark_identity_lost_fn=self.mark_transcript_identity_lost,
                            reset_identity_loss_fn=self._reset_identity_loss_confirmation,
                            is_untrusted_rotation_fn=self._is_untrusted_rotation,
                            rescue_jobs_fn=self._rescue_jobs,
                        )
                except asyncio.CancelledError:
                    raise
                except SourceContractError:
                    logger.exception(SOURCE_CONTRACT_FAILED, pid)
                    return
                except Exception:
                    logger.exception("observing %s failed", pid)
                await self._sleep(self.poll)
        finally:
            self._failures.clear_source_errors(pid, include_identity_lost=True)
            self._failures._identity_loss_replayed.discard(pid)
            self._attachments._reset_watch_state.discard(pid)
            self._attachments._receipt_candidates.pop(pid, None)
            self._attachments._sources.pop(pid, None)
            self._attachments.release_transcript(pid)
            try:
                await source.aclose()
            except (Exception, asyncio.CancelledError):
                logger.debug("closing source for %s failed", pid, exc_info=True)

    async def _watch_screen(self, pid: str, harness_name: str) -> None:
        """Derive status from the rendered screen, for a parser-less harness."""
        from theater.constants.observation import IDLE_CONFIRMATIONS

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
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("observing screen of %s failed", pid)
            await self._sleep(self.screen)

    def _open_source(self, pid: str, observer: HarnessObserver) -> Source | None:
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
        self._attachments.register_source(
            pid,
            source,
            clear_source_errors_fn=self._failures.clear_source_errors,
        )

    @staticmethod
    def _validate_batch(source: Source, batch: Batch) -> None:
        if not (batch.waiting and batch.attached is not None):
            return
        source.discard_attachment()
        raise SourceContractError(
            f"{type(source).__name__} returned a batch that is both waiting and attached"
        )

    # ---- legacy private method wrappers (explicit forwarding) ----------

    def _record_usage(self, pid: str, event: Event) -> bool:
        return self._reducer.record_usage(pid, event)

    def _apply(self, pid: str, batch: Batch, clock: QuietClock, turns: TurnAccumulator) -> bool:
        return self._reducer.apply(
            pid,
            batch,
            clock,
            turns,
            answer_turn_fn=self._answer_turn,
            settle_fn=self._settle,
            turn_result_fn=self._turn_result,
        )

    @staticmethod
    def _has_semantic_progress(batch: Batch) -> bool:
        return Reducer.has_semantic_progress(batch)

    def _unblock_on_semantic_progress(self, pid: str, batch: Batch) -> None:
        self._reducer.unblock_on_semantic_progress(pid, batch)

    async def _on_progress(
        self, pid: str, observer: HarnessObserver, batch: Batch, clock: QuietClock
    ) -> None:
        await self._reducer.on_progress(pid, observer, batch, clock)

    def _handle_source_error(self, pid: str, batch: Batch) -> None:
        self._failures.handle_source_error(pid, batch, finish_fn=self._finish)

    def _update_source_error(self, pid: str, batch: Batch) -> None:
        self._failures.update_source_error(pid, batch, finish_fn=self._finish)

    def _report_source_error(self, pid: str, batch: Batch) -> None:
        self._failures.report_source_error(pid, batch, finish_fn=self._finish)

    def _clear_source_error_on_progress(self, pid: str, batch: Batch) -> None:
        self._failures.clear_source_error_on_progress(pid, batch)

    def _clear_source_errors(self, pid: str, *, include_identity_lost: bool = False) -> None:
        self._failures.clear_source_errors(pid, include_identity_lost=include_identity_lost)

    def _turn_result(self, event, turn: Turn) -> tuple[str, str | object | None]:
        return self._reducer.turn_result(event, turn)

    async def _capture(self, pane: str) -> str | None:
        from theater.tmux import client as tmux

        try:
            return await tmux.run("capture-pane", "-p", "-t", pane, check=False)
        except Exception:
            return None

    async def _capture_for_reducer(self, pane: str) -> str | None:
        """Read _capture at call-time so instance monkeypatches take effect."""
        return await self._capture(pane)

    def _unblock(self, pid: str) -> None:
        self._reducer._unblock(pid)

    async def _on_quiet(
        self,
        pid: str,
        observer: HarnessObserver,
        source: Source,
        clock: QuietClock,
        turns: TurnAccumulator,
    ) -> None:
        await self._reducer.on_quiet(
            pid,
            observer,
            source,
            clock,
            turns,
            validate_batch_fn=self._validate_batch,
            report_source_error_fn=lambda p, b: self._failures.report_source_error(
                p, b, finish_fn=self._finish
            ),
            accept_attachment_fn=self._accept_attachment,
            apply_fn=lambda p, b, c, t: self._reducer.apply(
                p,
                b,
                c,
                t,
                answer_turn_fn=self._answer_turn,
                settle_fn=self._settle,
                turn_result_fn=self._turn_result,
            ),
            on_progress_fn=self._reducer.on_progress,
            evidence_bound_fn=self._evidence_is_bound_to_another_live_participant,
            confirm_identity_loss_fn=self._confirm_identity_loss,
            mark_identity_lost_fn=self.mark_transcript_identity_lost,
            reset_identity_loss_fn=self._reset_identity_loss_confirmation,
            is_untrusted_rotation_fn=self._is_untrusted_rotation,
            rescue_jobs_fn=self._rescue_jobs,
        )

    async def _screen_only(self, pid: str, observer: HarnessObserver, clock: QuietClock) -> None:
        await self._reducer.screen_only(pid, observer, clock)

    async def _screen_status_due(
        self, pid: str, observer: HarnessObserver, clock: QuietClock
    ) -> None:
        await self._reducer._screen_status_due(pid, observer, clock)

    def _is_untrusted_rotation(self, pid: str, attached: Attachment) -> bool:
        return self._attachments.is_untrusted_rotation(pid, attached)

    async def _screen_is_positively_working(self, pid: str, observer: HarnessObserver) -> bool:
        return await self._reducer.screen_is_positively_working(pid, observer)

    def _accept_attachment(self, pid: str, source: Source, batch: Batch) -> bool:
        return self._attachments.accept_attachment(
            pid,
            source,
            batch,
            handle_source_error_fn=self._handle_source_error,
            on_attach_fn=self._on_attach,
            clear_source_errors_fn=self._failures.clear_source_errors,
        )

    def _handle_attachment_ambiguity(self, pid: str, attached: Attachment) -> None:
        self._attachments._handle_attachment_ambiguity(pid, attached, self._handle_source_error)

    def _revoke_binding(self, location: str, owner: str) -> None:
        self._attachments._revoke_binding(location, owner)

    def _has_cwd_competitor(self, pid: str, collision_domain: str | None) -> bool:
        from theater.daemon.observation.identity import has_cwd_competitor

        return has_cwd_competitor(
            pid, collision_domain, self.store, self.registry, self._attachments._sources
        )

    def _trusted_dead_owner_blocks(self, pid: str, attached: Attachment) -> bool:
        from theater.daemon.observation.identity import trusted_dead_owner_blocks

        return trusted_dead_owner_blocks(pid, attached, self.store, self.registry)

    def history_is_ambiguous(self, pid: str, history: History) -> bool:
        return history_correlation_is_ambiguous(self.registry, pid, history)

    def transcript_receipt(self, pid: str, *, location: str, session_id: str) -> str:
        return self._attachments.transcript_receipt(
            pid,
            location=location,
            session_id=session_id,
            clear_source_errors_fn=self._failures.clear_source_errors,
        )

    def _stage_pending_receipt(self, pid: str, source: Source) -> None:
        self._attachments.stage_pending_receipt(
            pid,
            source,
            clear_source_errors_fn=self._failures.clear_source_errors,
        )

    def _stage_receipt_source(
        self, pid: str, source: Source, *, location: str, session_id: str
    ) -> str:
        return self._attachments._stage_receipt_source(
            pid,
            source,
            location=location,
            session_id=session_id,
            clear_source_errors_fn=self._failures.clear_source_errors,
        )

    def _on_attach(self, pid: str, attached: Attachment) -> None:
        self._attachments.on_attach(
            pid,
            attached,
            settle_fn=self._settle,
            settle_from_event_fn=self._settle_from_event,
            answer_turn_fn=self._answer_turn,
            turn_result_fn=self._turn_result,
        )

    def _settle_from_event(self, pid: str, event: Event) -> None:
        self._reducer.settle_from_event(
            pid, event, answer_turn_fn=self._answer_turn, turn_result_fn=self._turn_result
        )

    def _release_transcript(self, pid: str) -> None:
        self._attachments.release_transcript(pid)

    def _settle(self, pid: str, desired: Status) -> None:
        self._reducer.settle(pid, desired)

    async def _check_idle_screen(self, pid: str, observer: HarnessObserver) -> None:
        await self._reducer.check_idle_screen(pid, observer)

    def _apply_screen_reading(self, pid: str, reading) -> None:
        self._reducer.apply_screen_reading(pid, reading)

    async def _rescue_jobs(self, pid: str, observer: HarnessObserver, clock: QuietClock) -> None:
        await self._completion.rescue_jobs(
            pid, observer, clock, rescue_timeout=self.rescue, capture_fn=self._capture
        )

    def _answer_turn(
        self,
        pid: str,
        result_text: str,
        heard: Sequence[str] = (),
        *,
        raw_result: str | object | None = RAW_RESULT_UNSET,
    ) -> None:
        self._completion.answer_turn(pid, result_text, heard, raw_result=raw_result)

    def _release_jobs(
        self,
        pid: str,
        result_text: str,
        *,
        error_code: str | None = None,
        raw_result: str | object | None = RAW_RESULT_UNSET,
    ) -> None:
        self._completion.release_jobs(
            pid, result_text, error_code=error_code, raw_result=raw_result
        )

    def _finish_identity_lost_jobs(self, pid: str, result_text: str) -> None:
        self._completion.finish_identity_lost_jobs(pid, result_text)

    # ---- legacy instance-state properties for test monkeypatching ------

    @property
    def _unmatched(self) -> dict[str, int]:
        return self._completion._unmatched

    @_unmatched.setter
    def _unmatched(self, value: dict[str, int]) -> None:
        self._completion._unmatched = value

    @property
    def _source_errors(self) -> dict:
        return self._failures._source_errors

    @_source_errors.setter
    def _source_errors(self, value) -> None:
        self._failures._source_errors = value

    @property
    def _identity_lost(self) -> set[str]:
        return self._failures._identity_lost

    @_identity_lost.setter
    def _identity_lost(self, value: set[str]) -> None:
        self._failures._identity_lost = value

    @property
    def _identity_loss_replayed(self) -> set[str]:
        return self._failures._identity_loss_replayed

    @_identity_loss_replayed.setter
    def _identity_loss_replayed(self, value: set[str]) -> None:
        self._failures._identity_loss_replayed = value

    @property
    def _identity_loss_pending(self) -> dict:
        return self._failures._identity_loss_pending

    @_identity_loss_pending.setter
    def _identity_loss_pending(self, value: dict) -> None:
        self._failures._identity_loss_pending = value

    @property
    def _bound_transcripts(self) -> dict[str, str]:
        return self._attachments._bound_transcripts

    @_bound_transcripts.setter
    def _bound_transcripts(self, value: dict[str, str]) -> None:
        self._attachments._bound_transcripts = value

    @property
    def _binding_correlation(self) -> dict[str, str]:
        return self._attachments._binding_correlation

    @_binding_correlation.setter
    def _binding_correlation(self, value: dict[str, str]) -> None:
        self._attachments._binding_correlation = value

    @property
    def _binding_sessions(self) -> dict[str, str | None]:
        return self._attachments._binding_sessions

    @_binding_sessions.setter
    def _binding_sessions(self, value: dict[str, str | None]) -> None:
        self._attachments._binding_sessions = value

    @property
    def _sources(self) -> dict[str, Source]:
        return self._attachments._sources

    @_sources.setter
    def _sources(self, value: dict[str, Source]) -> None:
        self._attachments._sources = value

    @property
    def _receipt_candidates(self) -> dict[str, tuple[str, str]]:
        return self._attachments._receipt_candidates

    @_receipt_candidates.setter
    def _receipt_candidates(self, value: dict[str, tuple[str, str]]) -> None:
        self._attachments._receipt_candidates = value

    @property
    def _reset_watch_state(self) -> set[str]:
        return self._attachments._reset_watch_state

    @_reset_watch_state.setter
    def _reset_watch_state(self, value: set[str]) -> None:
        self._attachments._reset_watch_state = value

    def _evidence_is_bound_to_another_live_participant(
        self, pid: str, evidence: IdentityLossEvidence
    ) -> bool:
        return self._failures.evidence_is_bound_to_another_live(
            pid,
            evidence,
            bound_transcripts=self._attachments._bound_transcripts,
            binding_sessions=self._attachments._binding_sessions,
        )

    def _location_bound_to_another_live(self, pid: str, location: str) -> bool:
        return self._failures._location_bound_to_another_live(
            pid, location, self._attachments._bound_transcripts
        )

    def _session_id_bound_to_another_live(self, pid: str, session_id: str | None) -> bool:
        return self._failures._session_id_bound_to_another_live(
            pid,
            session_id,
            self._attachments._bound_transcripts,
            self._attachments._binding_sessions,
        )

    def _confirm_identity_loss(self, pid: str, evidence: IdentityLossEvidence) -> bool:
        return self._failures.confirm_identity_loss(pid, evidence)

    def _reset_identity_loss_confirmation(self, pid: str) -> None:
        self._failures.reset_identity_loss_confirmation(pid)

    def _end_turn_from_screen(self, pid: str, capture: str) -> None:
        self._reducer.end_turn_from_screen(pid, capture, answer_turn_fn=self._answer_turn)
