"""Live trajectory stream ingestion, history loading, and cache lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from theater.constants.daemon import BUS_PARTICIPANT_PAGE_MAX_LIMIT
from theater.constants.trajectory import (
    TRAJECTORY_BUS_DRAIN_BATCH,
    TRAJECTORY_CACHE_SWEEP_SECONDS,
    TRAJECTORY_MAX_COVERAGE_GAPS,
    TRAJECTORY_MUTABLE_UPDATE_COALESCE_MS,
    TRAJECTORY_PAGE_RECORD_LIMIT,
)
from theater.daemon.trajectory.cache import CacheStream, RecordChange, TrajectoryCache
from theater.daemon.trajectory.history import HistoryLoad, load_history, source_epoch_for
from theater.daemon.trajectory.merge import is_interaction, is_mutable, order_records
from theater.daemon.trajectory.project import project_batch, project_history_page
from theater.daemon.trajectory.theater_events import ALLOWLISTED_BUS_KINDS, project_bus_row
from theater.harness import normalize
from theater.harness.contracts.source import Batch
from theater.models import NotFound, Participant, Status, Tier
from theater.provenance import is_trusted_provenance
from theater.trajectory import (
    CoverageGap,
    PanelState,
    PanelStateInfo,
    TrajectoryCapabilities,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryParticipantState,
    TrajectoryRecord,
    TrajectoryStatus,
)
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    transcript_identity_recovery_message,
)

logger = logging.getLogger("theater.daemon.trajectory")


@dataclass(frozen=True, slots=True)
class CapturedBatch:
    serial: int
    batch: Batch


@dataclass(slots=True)
class TrajectoryStream:
    participant: Participant
    cache: CacheStream
    panel_state: PanelStateInfo
    source_epoch: str | None = None
    transcript_floor: str | None = None
    theater_floor: str | None = None
    source_before: str | None = None
    bus_before: int | None = None
    gaps: list[CoverageGap] = field(default_factory=list)
    declared_capabilities: TrajectoryCapabilities = field(default_factory=TrajectoryCapabilities)
    live_updates_observed: bool = False
    pending_live: list[CapturedBatch] = field(default_factory=list)
    followers: dict[int, asyncio.Event] = field(default_factory=dict)
    capture_serial: int = 0
    initialized: bool = False
    trusted: bool = False
    live_allowed: bool = False
    pending_wake: asyncio.TimerHandle | None = None
    initialization_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def ring(self):
        return self.cache.ring


class TrajectoryRuntime:
    """Own warm streams and ingest source or Theater-bus updates."""

    def __init__(self, store, registry, observer=None) -> None:
        self.store = store
        self.registry = registry
        self.observer = observer
        self.cache = TrajectoryCache()
        self.streams: dict[str, TrajectoryStream] = {}
        self._bus_queue: deque[dict] = deque()
        self._bus_scheduled = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._history_tasks: set[asyncio.Task] = set()
        self._ttl_task: asyncio.Task | None = None
        self._closed = False
        self._eviction_listener: Callable[[CacheStream], None] | None = None
        self._bus_listener_registered = False
        if observer is not None:
            setter = getattr(observer, "set_trajectory_capture", None)
            if callable(setter):
                setter(self.capture_batch)

    def set_eviction_listener(self, listener: Callable[[CacheStream], None]) -> None:
        self._eviction_listener = listener

    def set_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop

    def replace_cache(self, cache: TrajectoryCache) -> None:
        if self.streams:
            raise RuntimeError("cannot replace a trajectory cache with warm streams")
        self.cache = cache

    def capture_batch(self, participant_id: str, batch: Batch) -> None:
        if self._closed:
            return
        stream = self.streams.get(participant_id)
        if stream is None:
            return
        try:
            self.cache.touch(participant_id)
            stream.capture_serial += 1
            captured = CapturedBatch(stream.capture_serial, batch)
            if not stream.initialized:
                stream.pending_live.append(captured)
            else:
                self._apply_live(stream, captured, notify=True)
        except Exception:
            logger.exception("trajectory live capture failed for %s", participant_id)

    def create_stream(self, participant: Participant) -> TrajectoryStream:
        stream_id = uuid.uuid4().hex
        cache_stream = self.cache.add(participant.id, stream_id)
        stream = TrajectoryStream(
            participant=participant,
            cache=cache_stream,
            panel_state=PanelStateInfo(
                PanelState.WAITING,
                "loading trajectory history",
                participant_state(participant),
            ),
            declared_capabilities=self._declared_capabilities(participant),
        )
        self.streams[participant.id] = stream
        self._register_bus_listener()
        cache_stream.loading = True
        self._ensure_ttl_task()
        for evicted in self.cache.enforce(protected={participant.id}):
            self._discard_stream(evicted.participant_id, evicted)
        return stream

    async def initialize(self, stream: TrajectoryStream) -> None:
        async with stream.initialization_lock:
            if stream.initialized:
                return
            stream.cache.loading = True
            task = asyncio.create_task(
                load_history(
                    self,
                    stream.participant,
                    before=None,
                    limit=TRAJECTORY_PAGE_RECORD_LIMIT,
                )
            )
            self._history_tasks.add(task)
            historical_bus = self._bus_history(stream.participant.id, stream=stream)
            self._merge_bus_rows(stream, historical_bus, notify=False)
            stream.bus_before = (
                historical_bus[0]["id"]
                if len(historical_bus) >= BUS_PARTICIPANT_PAGE_MAX_LIMIT
                and type(historical_bus[0].get("id")) is int
                else None
            )
            try:
                result = await task
            finally:
                self._history_tasks.discard(task)
                stream.cache.loading = False
            self._drain_bus_queue()
            self._apply_history(stream, result, older=True)
            self._set_initial_state(stream, result)
            pending = tuple(stream.pending_live)
            stream.pending_live.clear()
            for captured in pending:
                self._apply_live(stream, captured, notify=False)
            stream.initialized = True
            self.cache.touch(stream.participant.id)
            for evicted in self.cache.enforce(protected={stream.participant.id}):
                self._discard_stream(evicted.participant_id, evicted)

    async def refresh(self, stream: TrajectoryStream) -> None:
        async with stream.initialization_lock:
            if not stream.initialized:
                return
            stream.cache.loading = True
            task = asyncio.create_task(
                load_history(
                    self,
                    stream.participant,
                    before=None,
                    limit=TRAJECTORY_PAGE_RECORD_LIMIT,
                )
            )
            self._history_tasks.add(task)
            try:
                result = await task
            finally:
                self._history_tasks.discard(task)
                stream.cache.loading = False
            gaps_changed = self._apply_history(stream, result, older=False)
            panel_changed = self._set_initial_state(stream, result, notify=True)
            if gaps_changed and not panel_changed:
                self.wake_followers(stream)
            self.cache.touch(stream.participant.id)

    async def load_older(
        self,
        stream: TrajectoryStream,
        *,
        source_before: str | None,
        bus_before: int | None,
        record_before: str | None,
        limit: int,
    ) -> tuple[str | None, int | None, tuple[TrajectoryRecord, ...]]:
        marker_exists = record_before is not None and any(
            record.record_id == record_before for record in stream.ring.records()
        )
        before_count = (
            count_records_before(stream.ring.records(), record_before)
            if record_before is not None
            else 0
        )
        needs_more = not marker_exists or before_count < limit
        loaded_records: list[TrajectoryRecord] = []
        if source_before is not None and needs_more:
            task = asyncio.create_task(
                load_history(
                    self,
                    stream.participant,
                    before=source_before,
                    limit=min(limit, TRAJECTORY_PAGE_RECORD_LIMIT),
                )
            )
            self._history_tasks.add(task)
            try:
                result = await task
            finally:
                self._history_tasks.discard(task)
            if result.trusted:
                records = project_history_page(
                    result.page,
                    participant_id=stream.participant.id,
                    source_epoch=result.source_epoch
                    or stream.source_epoch
                    or source_epoch_for(stream.participant, None),
                )
                loaded_records.extend(records)
                self._merge_records(stream, records, notify=False)
                source_before = result.page.older_cursor if result.page.has_older else None
                stream.transcript_floor = result.page.older_cursor or result.page.cursor
            else:
                self._add_history_failure(stream, result)
                reason = result.message or result.page.error or "history source failed"
                self._set_panel(
                    stream,
                    PanelState.STALE,
                    f"older transcript history is unavailable: {reason}; retry this older page",
                    notify=False,
                )
        if bus_before is not None and needs_more:
            rows = self._bus_history(stream.participant.id, before_id=bus_before, stream=stream)
            records = tuple(
                record
                for row in rows
                if (record := project_bus_row(row, stream.participant.id)) is not None
            )
            loaded_records.extend(records)
            self._merge_records(stream, records, notify=False)
            bus_before = rows[0]["id"] if len(rows) >= BUS_PARTICIPANT_PAGE_MAX_LIMIT else None
            self._update_theater_floor(stream, records)
        return source_before, bus_before, tuple(loaded_records)

    def refresh_participant(self, stream: TrajectoryStream, *, notify: bool = False) -> bool:
        getter = getattr(self.registry, "get", None)
        if not callable(getter):
            return False
        try:
            participant = getter(stream.participant.id)
        except NotFound:
            return self._replace_panel(
                stream,
                PanelStateInfo(
                    PanelState.UNAVAILABLE,
                    (
                        "participant is missing; refresh the participant tree and select an "
                        "existing id"
                    ),
                    TrajectoryParticipantState.MISSING,
                ),
                notify=notify,
            )
        if isinstance(participant, Participant):
            stream.participant = participant
            state = participant_state(participant)
            message = stream.panel_state.message
            if state is TrajectoryParticipantState.DEAD:
                message = "participant is dead; live trajectory updates have stopped"
            elif state is TrajectoryParticipantState.EXTERNAL:
                message = "participant is external; live trajectory updates are unavailable"
            return self._replace_panel(
                stream,
                PanelStateInfo(
                    stream.panel_state.state,
                    message,
                    state,
                ),
                notify=notify,
            )
        return False

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._unregister_bus_listener()
        if self.observer is not None:
            setter = getattr(self.observer, "set_trajectory_capture", None)
            if callable(setter):
                setter(None)
        if self._ttl_task is not None:
            self._ttl_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ttl_task
            self._ttl_task = None
        tasks = [task for task in self._history_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for stream in tuple(self.streams.values()):
            if stream.pending_wake is not None:
                stream.pending_wake.cancel()
            self.wake_followers(stream)
        self.streams.clear()
        self._bus_queue.clear()
        self.cache.clear()

    def _ensure_ttl_task(self) -> None:
        if self._ttl_task is None and self._loop is not None:
            self._ttl_task = self._loop.create_task(self._ttl_loop())

    async def _ttl_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(TRAJECTORY_CACHE_SWEEP_SECONDS)
            for expired in self.cache.expire():
                self._discard_stream(expired.participant_id, expired)

    def _apply_history(self, stream: TrajectoryStream, result: HistoryLoad, *, older: bool) -> bool:
        page = result.page
        if result.trusted:
            stream.trusted = True
            stream.live_allowed = is_trusted_provenance(page.provenance) or is_trusted_provenance(
                stream.participant.session_correlation
            )
            if result.source_epoch is not None:
                if stream.source_epoch is not None and stream.source_epoch != result.source_epoch:
                    self._add_boundary(stream, stream.source_epoch, result.source_epoch)
                    self._add_gap(stream, "transcript", "transcript session rotated")
                stream.source_epoch = result.source_epoch
            records = project_history_page(
                page,
                participant_id=stream.participant.id,
                source_epoch=stream.source_epoch or source_epoch_for(stream.participant, None),
            )
            self._merge_records(stream, records, notify=False)
            if page.older_cursor is not None and page.has_older:
                stream.source_before = page.older_cursor
            elif older:
                stream.source_before = None
            if page.cursor is not None:
                stream.transcript_floor = page.older_cursor or page.cursor
        else:
            return self._add_history_failure(stream, result)
        return False

    def _set_initial_state(
        self, stream: TrajectoryStream, result: HistoryLoad, *, notify: bool = False
    ) -> bool:
        self.refresh_participant(stream, notify=notify)
        current_participant_state = stream.panel_state.participant_state
        has_transcript = any(
            not record.record_id.startswith("bus:") for record in stream.ring.records()
        )
        page = result.page
        if result.trusted and (
            page.location is not None
            or page.events
            or page.trajectory
            or has_transcript
            or (stream.live_allowed and has_transcript)
        ):
            state = PanelState.READY
            message = result.message
        elif result.trusted:
            state = PanelState.WAITING
            message = result.message or "the transcript source is waiting for its first record"
        elif page.error_code == TRANSCRIPT_IDENTITY_LOST_CODE or result.ambiguous:
            state = (
                PanelState.UNAVAILABLE
                if stream.participant.status is Status.DEAD
                else PanelState.UNTRUSTED
            )
            message = result.message
        elif page.error_code is None and not is_trusted_provenance(page.provenance):
            state = PanelState.UNTRUSTED
            message = result.message or "transcript identity is not trusted"
        elif has_transcript:
            state = PanelState.STALE
            message = f"transcript history is unavailable; cached records remain ({result.message})"
        else:
            state = PanelState.UNAVAILABLE
            message = result.message or "trajectory history is unavailable"
        return self._replace_panel(
            stream,
            PanelStateInfo(state, message, current_participant_state),
            notify=notify,
        )

    def _apply_live(
        self, stream: TrajectoryStream, captured: CapturedBatch, *, notify: bool
    ) -> None:
        batch = captured.batch
        attachment = batch.attached
        if attachment is not None:
            if not is_trusted_provenance(attachment.correlation):
                stream.live_allowed = False
                gap_added = self._add_gap(
                    stream, "transcript", "live transcript attachment is untrusted"
                )
                panel_changed = self._set_panel(
                    stream,
                    PanelState.UNTRUSTED,
                    "live transcript identity is untrusted",
                    notify=notify,
                )
                if gap_added and notify and not panel_changed:
                    self.wake_followers(stream)
                return
            stream.live_allowed = True
            epoch = source_epoch_for(stream.participant, attachment.location)
            if stream.source_epoch is not None and stream.source_epoch != epoch:
                self._add_boundary(stream, stream.source_epoch, epoch)
                self._add_gap(
                    stream, "transcript", "transcript session rotated", stream.source_epoch, epoch
                )
            stream.source_epoch = epoch
        if not stream.live_allowed:
            return
        if batch.error_code is not None:
            reason = batch.error or batch.error_code
            gap_added = self._add_gap(stream, "transcript", reason)
            if batch.error_code == TRANSCRIPT_IDENTITY_LOST_CODE:
                panel_changed = self._set_panel(
                    stream,
                    PanelState.UNTRUSTED,
                    transcript_identity_recovery_message(stream.participant.id, reason),
                    notify=notify,
                )
            else:
                panel_changed = self._set_panel(
                    stream,
                    PanelState.STALE,
                    f"live trajectory read failed: {reason}; request a fresh snapshot to retry",
                    notify=notify,
                )
            if gap_added and notify and not panel_changed:
                self.wake_followers(stream)
            return
        epoch = stream.source_epoch or source_epoch_for(stream.participant, None)
        records = project_batch(batch, participant_id=stream.participant.id, source_epoch=epoch)
        changes = self._merge_records(stream, records, notify=notify)
        if changes:
            stream.live_updates_observed = True
        if records and stream.panel_state.state in {
            PanelState.WAITING,
            PanelState.UNAVAILABLE,
            PanelState.STALE,
            PanelState.UNTRUSTED,
        }:
            self._set_panel(
                stream,
                PanelState.READY,
                "live transcript records are available",
                notify=False,
            )

    def _add_history_failure(self, stream: TrajectoryStream, result: HistoryLoad) -> bool:
        reason = result.message or result.page.error or "history source failed"
        return self._add_gap(stream, "transcript", reason)

    def _add_boundary(self, stream: TrajectoryStream, old: str, new: str) -> None:
        record = TrajectoryRecord(
            record_id=f"session-boundary:{old}:{new}",
            revision=0,
            participant_id=stream.participant.id,
            source_epoch=new,
            lane=TrajectoryLane.THEATER,
            kind=TrajectoryKind.SESSION_BOUNDARY,
            source="daemon",
            summary="Transcript session boundary",
            status=TrajectoryStatus.COMPLETED,
        )
        self._merge_records(stream, (record,), notify=True)

    @staticmethod
    def _add_gap(
        stream: TrajectoryStream,
        source: str,
        reason: str,
        start: str | None = None,
        end: str | None = None,
    ) -> bool:
        gap = CoverageGap(source, reason, start=start, end=end)
        if gap not in stream.gaps:
            stream.gaps.append(gap)
            del stream.gaps[:-TRAJECTORY_MAX_COVERAGE_GAPS]
            return True
        return False

    def _set_panel(
        self,
        stream: TrajectoryStream,
        state: PanelState,
        message: str,
        *,
        notify: bool,
    ) -> bool:
        return self._replace_panel(
            stream,
            PanelStateInfo(state, message, stream.panel_state.participant_state),
            notify=notify,
        )

    def _replace_panel(
        self, stream: TrajectoryStream, panel_state: PanelStateInfo, *, notify: bool
    ) -> bool:
        if stream.panel_state == panel_state:
            return False
        stream.panel_state = panel_state
        if notify:
            self.wake_followers(stream)
        return True

    def _merge_records(
        self,
        stream: TrajectoryStream,
        records: Iterable[TrajectoryRecord],
        *,
        notify: bool,
    ) -> tuple[RecordChange, ...]:
        values = tuple(records)
        previous = {record.record_id: stream.ring.get(record.record_id) for record in values}
        changes = stream.ring.merge(values)
        if not changes:
            return ()
        self.cache.touch(stream.participant.id)
        for evicted in self.cache.enforce(protected={stream.participant.id}):
            self._discard_stream(evicted.participant_id, evicted)
        if notify:
            immediate = any(
                is_interaction(change.record)
                or not is_mutable(change.record, previous.get(change.record.record_id))
                for change in changes
            )
            if immediate:
                if stream.pending_wake is not None:
                    stream.pending_wake.cancel()
                    stream.pending_wake = None
                self.wake_followers(stream)
            else:
                self._schedule_wake(stream)
        return changes

    def _schedule_wake(self, stream: TrajectoryStream) -> None:
        if self._loop is None or stream.pending_wake is not None:
            return
        stream.pending_wake = self._loop.call_later(
            TRAJECTORY_MUTABLE_UPDATE_COALESCE_MS / 1000.0,
            self._flush_wake,
            stream,
        )

    def _flush_wake(self, stream: TrajectoryStream) -> None:
        stream.pending_wake = None
        if not self._closed:
            self.wake_followers(stream)

    @staticmethod
    def wake_followers(stream: TrajectoryStream) -> None:
        for event in tuple(stream.followers.values()):
            event.set()

    def _merge_bus_rows(
        self, stream: TrajectoryStream, rows: Iterable[dict], *, notify: bool
    ) -> None:
        values = tuple(rows)
        records = tuple(
            record
            for row in values
            if (record := project_bus_row(row, stream.participant.id)) is not None
        )
        self._merge_records(stream, records, notify=notify)
        self._update_theater_floor(stream, records)
        if any(row.get("kind") == "participant.dead" for row in values):
            self.refresh_participant(stream, notify=notify)
            self._replace_panel(
                stream,
                PanelStateInfo(
                    stream.panel_state.state,
                    "participant is dead; live trajectory updates have stopped",
                    TrajectoryParticipantState.DEAD,
                ),
                notify=notify,
            )

    @staticmethod
    def _update_theater_floor(
        stream: TrajectoryStream, records: Iterable[TrajectoryRecord]
    ) -> None:
        values = tuple(records)
        if not values:
            return
        floor = min(record.raw_index for record in values)
        if stream.theater_floor is not None and stream.theater_floor.startswith("bus:"):
            with contextlib.suppress(ValueError):
                floor = min(floor, int(stream.theater_floor.removeprefix("bus:")))
        stream.theater_floor = f"bus:{floor}"

    def _on_bus_row(self, row: dict) -> None:
        if self._closed or not self.streams:
            return
        try:
            self._bus_queue.append(dict(row))
            if self._loop is not None and not self._bus_scheduled:
                self._bus_scheduled = True
                self._loop.call_soon(self._drain_bus_queue)
        except Exception:
            logger.exception("trajectory bus capture failed")

    def _declared_capabilities(self, participant: Participant) -> TrajectoryCapabilities:
        harnesses = getattr(self.observer, "harnesses", {})
        harness = (
            harnesses.get(normalize(participant.harness))
            if isinstance(harnesses, Mapping)
            else None
        )
        declared = getattr(getattr(harness, "observer", None), "trajectory_capabilities", None)
        return (
            declared if isinstance(declared, TrajectoryCapabilities) else TrajectoryCapabilities()
        )

    def _register_bus_listener(self) -> None:
        if not self._bus_listener_registered:
            self.store.register_bus_listener(self._on_bus_row)
            self._bus_listener_registered = True

    def _unregister_bus_listener(self) -> None:
        if self._bus_listener_registered:
            self.store.unregister_bus_listener(self._on_bus_row)
            self._bus_listener_registered = False

    def _drain_bus_queue(self) -> None:
        self._bus_scheduled = False
        for _ in range(min(len(self._bus_queue), TRAJECTORY_BUS_DRAIN_BATCH)):
            row = self._bus_queue.popleft()
            if row.get("kind") not in ALLOWLISTED_BUS_KINDS:
                continue
            affected = {
                value for value in (row.get("from_id"), row.get("to_id")) if isinstance(value, str)
            } & self.streams.keys()
            for participant_id in affected:
                stream = self.streams.get(participant_id)
                if stream is not None:
                    try:
                        self._merge_bus_rows(stream, (row,), notify=True)
                    except Exception:
                        logger.exception("trajectory bus projection failed for %s", participant_id)
        if self._bus_queue and self._loop is not None and not self._closed:
            self._bus_scheduled = True
            self._loop.call_soon(self._drain_bus_queue)

    def _bus_history(
        self,
        participant_id: str,
        *,
        before_id: int | None = None,
        stream: TrajectoryStream | None = None,
    ) -> list[dict]:
        try:
            return self.store.bus_page_for_participant(
                participant_id,
                before_id=before_id,
                limit=BUS_PARTICIPANT_PAGE_MAX_LIMIT,
                kinds=ALLOWLISTED_BUS_KINDS,
            )
        except Exception as exc:
            logger.debug("trajectory bus history unavailable: %s", exc)
            if stream is not None:
                self._add_gap(stream, "theater", f"theater bus history unavailable: {exc}")
            return []

    def _discard_stream(self, participant_id: str, cache_stream: CacheStream) -> None:
        current = self.streams.get(participant_id)
        if current is not None and current.cache is cache_stream:
            self.streams.pop(participant_id, None)
            self.wake_followers(current)
            if current.pending_wake is not None:
                current.pending_wake.cancel()
            if self._eviction_listener is not None:
                self._eviction_listener(cache_stream)
        self.cache.remove(participant_id)
        if not self.streams:
            self._unregister_bus_listener()


def count_records_before(records: tuple[TrajectoryRecord, ...], marker: str) -> int:
    ordered = order_records(records)
    try:
        return next(index for index, record in enumerate(ordered) if record.record_id == marker)
    except StopIteration:
        return 0


def participant_state(participant: Participant) -> TrajectoryParticipantState:
    if participant.status is Status.DEAD:
        return TrajectoryParticipantState.DEAD
    if participant.tier is Tier.EXTERNAL:
        return TrajectoryParticipantState.EXTERNAL
    return TrajectoryParticipantState.LIVE


__all__ = ["TrajectoryRuntime", "TrajectoryStream", "participant_state"]
