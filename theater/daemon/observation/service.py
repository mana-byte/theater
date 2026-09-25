"""Observer lifecycle and watch orchestration over explicitly wired collaborators."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from functools import partial

from theater import timing
from theater.config import ObserverSection
from theater.constants.observation import OBSERVATION_FAILURE_GRACE, SOURCE_CONTRACT_FAILED
from theater.daemon.observation.attachment import AttachmentManager
from theater.daemon.observation.batches import BatchApplication
from theater.daemon.observation.binding import TranscriptBinding
from theater.daemon.observation.completion import CompletionTracker
from theater.daemon.observation.completion_gate import CompletionGate
from theater.daemon.observation.evidence import TerminalEvidenceRouting
from theater.daemon.observation.failures import FailureTracker
from theater.daemon.observation.identity_loss import IdentityLossWiring
from theater.daemon.observation.live import EvidenceSink, LiveObservationHub
from theater.daemon.observation.process import ObservationProcess, observation_process
from theater.daemon.observation.reducer import QuietClock, Reducer
from theater.daemon.observation.screen_reading import ScreenReading
from theater.daemon.observation.sources import SourceChannels
from theater.daemon.observation.state_views import CollaboratorStateViews
from theater.daemon.observation.supervision import WatchSupervision
from theater.daemon.observation.turns import TurnAccumulator
from theater.daemon.registry import Registry
from theater.harness import HARNESSES, Harness
from theater.harness.channels.health import ChannelHealthTracker
from theater.harness.channels.hooks import HookRuntime
from theater.harness.channels.otel import NativeOtelRuntime
from theater.harness.contracts.channels import ChannelHealth
from theater.harness.contracts.runtime import NativeTurnOutcome
from theater.harness.source import Batch, Source, SourceContractError
from theater.harness.transcript.observer import open_participant_source
from theater.models import Status
from theater.models import now as wall_now
from theater.observability.catalog import OBSERVATION_GAP

logger = logging.getLogger("theater.observer")

_DEFAULTS = ObserverSection()

POLL_INTERVAL = _DEFAULTS.poll_interval
RELOCATE_TIMEOUT = _DEFAULTS.relocate_timeout
AWAITING_INPUT_TIMEOUT = _DEFAULTS.awaiting_input_timeout
SEARCH_INTERVAL = _DEFAULTS.search_interval
SYNC_INTERVAL = _DEFAULTS.sync_interval
SCREEN_INTERVAL = _DEFAULTS.screen_interval
RESCUE_TIMEOUT = _DEFAULTS.rescue_timeout


def _batch_carries_observation(batch: Batch) -> bool:
    """Whether one read produced new observations worth measuring gaps between.

    Any forward movement counts: treating consumed input as silence would fake an
    observation gap exactly while work is happening.
    """
    return (
        bool(batch.events)
        or bool(batch.terminal_evidence)
        or batch.progressed
        or batch.status is not None
    )


class Observer(
    WatchSupervision,
    SourceChannels,
    TerminalEvidenceRouting,
    BatchApplication,
    ScreenReading,
    TranscriptBinding,
    IdentityLossWiring,
    CompletionGate,
    CollaboratorStateViews,
):
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
        agent_telemetry=None,
        hook_runtime: HookRuntime | None = None,
        otel_runtime: NativeOtelRuntime | None = None,
        live_hub: LiveObservationHub | None = None,
        wall_clock: Callable[[], float] = wall_now,
        monotonic_clock: Callable[[], float] = time.monotonic,
        source_factory: Callable[..., Source] = open_participant_source,
        failure_grace: float = OBSERVATION_FAILURE_GRACE,
    ):
        self._wall_now = wall_clock
        self._readiness_since = wall_clock()
        self._monotonic = monotonic_clock
        self._open_participant_source = source_factory
        self._failure_grace = failure_grace
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
        self.agent_telemetry = agent_telemetry
        self.hook_runtime = hook_runtime
        self.otel_runtime = otel_runtime
        # The live-channel composition seam: lifecycle code registers a
        # participant's runtime live source here (never by importing plugin
        # internals) and calls ``self.live.wake(pid)`` when data arrives.
        self.live = (
            LiveObservationHub(on_change=self._on_live_change) if live_hub is None else live_hub
        )
        self._tasks: dict[str, asyncio.Task] = {}
        self._restarts: set[asyncio.Task[None]] = set()
        self._restart_pending: set[str] = set()
        self._retired: set[str] = set()
        self._unobservable: set[str] = set()
        self._pending_transcripts: set[str] = set()
        self._source_processes: dict[str, ObservationProcess | None] = {}
        self._channel_health: dict[str, tuple[ChannelHealth, ...]] = {}
        self._primary_channel_health: dict[tuple[str, str], ChannelHealthTracker] = {}
        self._supervisor: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._trajectory_capture = None
        self._terminal_evidence_provider = None
        # Terminal evidence retained by the observer itself, for live-only
        # wiring whose source drains outcomes it cannot replay. Keyed by
        # exact native session/turn identity so retries never grow the set;
        # each value keeps the backend generation it was captured under.
        self._pending_evidence: dict[
            str,
            OrderedDict[
                tuple[int, str, str],
                tuple[EvidenceSink | None, Source, NativeTurnOutcome],
            ],
        ] = {}

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
            telemetry_fn=(agent_telemetry.record_batch if agent_telemetry is not None else None),
        )

    def _grace(self) -> float:
        return self._failure_grace

    def set_trajectory_capture(self, callback) -> None:
        """Install the optional synchronous trajectory batch sink."""
        self._trajectory_capture = callback

    def set_terminal_evidence_provider(self, provider) -> None:
        """Install the daemon-owned source for verified provider screen facts."""
        self._terminal_evidence_provider = provider

    def _capture_trajectory(self, pid: str, batch: Batch) -> None:
        callback = self._trajectory_capture
        if callback is None:
            return
        try:
            callback(pid, batch)
        except Exception:
            logger.exception("trajectory capture failed for %s", pid)

    def _discard_agent_telemetry(self, pid: str) -> None:
        if self.agent_telemetry is None:
            return
        try:
            self.agent_telemetry.discard(pid)
        except Exception:
            logger.exception("discarding agent telemetry failed for %s", pid)

    async def _watch(self, pid: str, harness_name: str) -> None:
        try:
            await self._watch_source(pid, harness_name)
        finally:
            self._source_processes.pop(pid, None)
            self._discard_agent_telemetry(pid)

    async def _watch_source(self, pid: str, harness_name: str) -> None:  # noqa: PLR0912, PLR0915
        observer = self.harnesses[harness_name].observer
        participant = self.store.get_participant(pid)
        if self._stopping.is_set() or participant is None or participant.status is Status.DEAD:
            return
        registration = self.live.registration_for(pid)
        opened_durable = bool(
            observer.has_transcript and participant is not None and participant.cwd is not None
        )
        if opened_durable and registration is None and participant is not None:
            # Native sources follow their runtime lifecycle, not provider process refreshes.
            self._source_processes[pid] = observation_process(self.store, participant)
        try:
            source = self._open_source_for_registration(pid, observer, registration)
        except Exception as exc:
            participant = self.store.get_participant(pid)
            if participant is not None:
                detail = str(exc) or type(exc).__name__
                self._warn_unobservable(
                    pid,
                    participant,
                    reason=f"its observation source could not be opened: {detail}",
                )
            return
        if source is None:
            return
        finish_fn = partial(self._finish, registration=registration)
        self._record_channel_health(pid, source)
        if opened_durable:
            self._restore_transcript_identity_loss(pid)
        clock = QuietClock()
        turns = TurnAccumulator()
        # Observation-gap reference, local to this watch and generation so a replacement never
        # emits a cross-watch spike. Measurement only: never touches wakeups, polling,
        # backpressure, evidence acknowledgement, or quiet timers.
        last_live_observation_at: float | None = None
        # Live wiring makes the watch loop wakeable: data arriving between
        # polls ends the sleep promptly. The poll interval remains the
        # fallback, so a participant without a wake producer behaves exactly
        # as before.
        wake = self.live.wake_signal(pid)
        try:
            if opened_durable:
                try:
                    self._register_source(pid, source)
                except SourceContractError:
                    logger.exception(SOURCE_CONTRACT_FAILED, pid)
                    return
            while not self._stopping.is_set():
                next_poll = self.poll
                batch: Batch | None = None
                # Batches applied this iteration, each evidence-routed exactly once at the end;
                # ``applied`` means the outer batch applied cleanly, so its checkpoint may persist.
                inner: list[Batch] = []
                applied = False
                try:
                    if pid in self._attachments._reset_watch_state:
                        self._attachments._reset_watch_state.discard(pid)
                        clock = QuietClock()
                        turns = TurnAccumulator()
                    # Retained evidence routes before anything else in the
                    # iteration, and before any checkpoint acknowledgement.
                    if not await self._flush_pending_evidence(pid):
                        # Enforce capacity before consumption. Until every
                        # observer-retained outcome routes, do not drain the
                        # source again; later evidence stays behind the
                        # source's own bounded backpressure boundary.
                        await self._sleep(self.poll, wake)
                        continue
                    if not self._persist_pending_source_checkpoint(pid, source):
                        await self._sleep(self.poll, wake)
                        continue
                    if opened_durable and self.transcript_identity_lost(pid):
                        self._sweep_identity_lost_grace(pid, registration=registration)
                        await self._screen_only(
                            pid,
                            observer,
                            clock,
                        )
                        await self._sleep(self.search, wake)
                        continue
                    # Race-safe consume: data arriving during the read below
                    # re-sets the signal, so its wake is never lost.
                    if wake is not None:
                        wake.consume()
                    batch = await self._read_source(pid, source)
                    if registration is not None and _batch_carries_observation(batch):
                        # Fail-open measurement: a broken clock, bridge, or
                        # batch read skips the sample and leaves the previous
                        # reference — the watch loop itself never changes.
                        with contextlib.suppress(Exception):
                            observed_at = self._monotonic()
                            if last_live_observation_at is not None:
                                timing.emit(
                                    OBSERVATION_GAP,
                                    (observed_at - last_live_observation_at) * 1000.0,
                                )
                            last_live_observation_at = observed_at
                    if batch.has_more:
                        next_poll = 0
                    self._validate_batch(source, batch)
                    if opened_durable:
                        if (
                            batch.waiting
                            and batch.error_code is None
                            and pid not in self._attachments._bound_transcripts.values()
                        ):
                            if pid not in self._pending_transcripts:
                                logger.info("waiting for first transcript id=%s", pid)
                            self._pending_transcripts.add(pid)
                        else:
                            self._pending_transcripts.discard(pid)
                    if batch.waiting:
                        self._capture_trajectory(pid, batch)
                        self._failures.update_source_error(pid, batch, finish_fn=finish_fn)
                        if await self._route_terminal_evidence(pid, source, batch, registration):
                            self._ack_terminal_evidence(source)
                            self._persist_pending_source_checkpoint(pid, source)
                        await self._screen_only(
                            pid,
                            observer,
                            clock,
                            source_status=batch.status,
                        )
                        await self._sleep(self.search, wake)
                        continue
                    self._failures.report_source_error(pid, batch, finish_fn=finish_fn)
                    if not opened_durable:
                        self._capture_trajectory(pid, batch)
                        # Same restatement rule as the reducer: a status
                        # settle needs progress, or it walks over the screen
                        # arm's awaiting verdict between polls.
                        if batch.status is not None and (batch.progressed or batch.events):
                            self._settle(pid, batch.status)
                        if await self._route_terminal_evidence(pid, source, batch, registration):
                            self._ack_terminal_evidence(source)
                            self._persist_pending_source_checkpoint(pid, source)
                        await self._screen_only(
                            pid,
                            observer,
                            clock,
                            source_status=batch.status,
                        )
                        await self._sleep(self.poll, wake)
                        continue
                    if not self._accept_attachment(pid, source, batch, registration=registration):
                        # Attachment was rejected: no staged semantics to
                        # persist, but exact evidence still routes first.
                        await self._screen_only(
                            pid,
                            observer,
                            clock,
                            source_status=batch.status,
                        )
                        if await self._route_terminal_evidence(pid, source, batch, registration):
                            self._ack_terminal_evidence(source)
                        await self._sleep(self.search, wake)
                        continue
                    self._capture_trajectory(pid, batch)
                    self._failures.clear_source_error_on_progress(pid, batch)
                    if self._apply_source_batch(
                        pid, source, batch, clock, turns, registration=registration
                    ):
                        applied = True
                        self._reducer.unblock_on_semantic_progress(pid, batch)
                        await self._reducer.on_progress(pid, observer, batch, clock)
                    else:
                        await self._reducer.on_quiet(
                            pid,
                            observer,
                            source,
                            clock,
                            turns,
                            source_status=batch.status,
                            validate_batch_fn=self._validate_batch,
                            report_source_error_fn=lambda p, b: self._failures.report_source_error(
                                p, b, finish_fn=finish_fn
                            ),
                            accept_attachment_fn=partial(
                                self._accept_attachment, registration=registration
                            ),
                            apply_fn=lambda p, b, c, t, inner=inner: self._apply_and_collect(
                                p, source, b, c, t, inner, registration
                            ),
                            on_progress_fn=self._reducer.on_progress,
                            evidence_bound_fn=self._evidence_is_bound_to_another_live_participant,
                            confirm_identity_loss_fn=self._confirm_identity_loss,
                            mark_identity_lost_fn=partial(
                                self.mark_transcript_identity_lost, registration=registration
                            ),
                            reset_identity_loss_fn=self._reset_identity_loss_confirmation,
                            is_untrusted_rotation_fn=self._is_untrusted_rotation,
                            rescue_jobs_fn=partial(self._rescue_jobs, registration=registration),
                        )
                        applied = True
                except asyncio.CancelledError:
                    # Replacement cancels us before closing the source: move drained outcomes
                    # to observer retention first; the hybrid's copy dies, the runtime dedupes.
                    if registration is not None:
                        if batch is not None and batch.terminal_evidence:
                            self._retain_terminal_evidence(pid, source, batch, registration)
                        for extra in inner:
                            if extra.terminal_evidence:
                                self._retain_terminal_evidence(pid, source, extra, registration)
                        self._retain_source_terminal_evidence(pid, source, registration)
                    raise
                except SourceContractError:
                    if batch is not None and await self._route_terminal_evidence(
                        pid, source, batch, registration
                    ):
                        # The contract failed before any apply, so only the
                        # evidence is released; the cursor is not persisted.
                        self._ack_terminal_evidence(source)
                    logger.exception(SOURCE_CONTRACT_FAILED, pid)
                    return
                except Exception:
                    logger.exception("observing %s failed", pid)
                # Exact evidence completes its job even if event application failed (the sink
                # persists first, idempotently). Ack after every outcome routed; persist if applied.
                routed = True
                if batch is not None:
                    routed = await self._route_terminal_evidence(pid, source, batch, registration)
                for extra in inner:
                    if not await self._route_terminal_evidence(pid, source, extra, registration):
                        routed = False
                if routed:
                    self._ack_terminal_evidence(source)
                    if applied:
                        self._persist_pending_source_checkpoint(pid, source)
                await self._sleep(next_poll, wake)
        finally:
            self._pending_transcripts.discard(pid)
            self._channel_health.pop(pid, None)
            self._clear_primary_channel_health(pid)
            self._failures.clear_source_errors(pid, include_identity_lost=opened_durable)
            self._attachments._reset_watch_state.discard(pid)
            if opened_durable:
                self._failures._identity_loss_replayed.discard(pid)
                self._attachments._receipt_candidates.pop(pid, None)
                self._attachments._sources.pop(pid, None)
                self._attachments.release_transcript(pid)
            try:
                await source.aclose()
            except (Exception, asyncio.CancelledError):
                logger.debug("closing source for %s failed", pid, exc_info=True)
