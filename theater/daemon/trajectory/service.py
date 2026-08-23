"""Lazy daemon-owned trajectory streams, live capture, and long-poll follow."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import uuid
from collections import deque
from dataclasses import dataclass, field

from theater.constants.daemon import BUS_PARTICIPANT_PAGE_MAX_LIMIT
from theater.constants.trajectory import (
    TRAJECTORY_CURSOR_MAX_BYTES,
    TRAJECTORY_FOLLOW_TIMEOUT_SECONDS,
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
    TRAJECTORY_MUTABLE_UPDATE_COALESCE_MS,
    TRAJECTORY_PAGE_RECORD_LIMIT,
    TRAJECTORY_RESPONSE_MAX_BYTES,
)
from theater.daemon.trajectory.cache import CacheStream, RecordChange, TrajectoryCache
from theater.daemon.trajectory.history import HistoryLoad, load_history, source_epoch_for
from theater.daemon.trajectory.merge import (
    groups_for_records,
    is_interaction,
    is_mutable,
    order_records,
)
from theater.daemon.trajectory.project import project_batch, project_history_page
from theater.daemon.trajectory.theater_events import ALLOWLISTED_BUS_KINDS, project_bus_row
from theater.harness.contracts.source import Batch
from theater.models import BadRequest, NotFound, Participant, Status, Tier
from theater.provenance import is_trusted_provenance
from theater.trajectory import (
    CoverageGap,
    PanelState,
    PanelStateInfo,
    TrajectoryCoverage,
    TrajectoryDelta,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryPage,
    TrajectoryParticipantState,
    TrajectoryRecord,
    TrajectoryStatus,
    TrajectoryUpsert,
)
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    transcript_identity_recovery_message,
)

logger = logging.getLogger("theater.daemon.trajectory")


@dataclass(frozen=True, slots=True)
class _CapturedBatch:
    serial: int
    batch: Batch


@dataclass(frozen=True, slots=True)
class _OlderState:
    daemon_epoch: str
    stream_id: str
    source_before: str | None
    bus_before: int | None
    record_before: str | None


@dataclass(slots=True)
class _Stream:
    participant: Participant
    cache: CacheStream
    panel_state: PanelStateInfo
    source_epoch: str | None = None
    transcript_floor: str | None = None
    theater_floor: str | None = None
    source_before: str | None = None
    bus_before: int | None = None
    gaps: list[CoverageGap] = field(default_factory=list)
    pending_live: list[_CapturedBatch] = field(default_factory=list)
    followers: dict[int, asyncio.Event] = field(default_factory=dict)
    capture_serial: int = 0
    watermark: int = 0
    bus_watermark: int = 0
    initialized: bool = False
    trusted: bool = False
    live_allowed: bool = False
    pending_wake: asyncio.TimerHandle | None = None
    initialization_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def ring(self):
        return self.cache.ring


class TrajectoryService:
    """The daemon composition root for all trajectory state."""

    def __init__(self, store, registry, observer=None):
        self.store = store
        self.registry = registry
        self.observer = observer
        self.daemon_epoch = uuid.uuid4().hex
        self.cache = TrajectoryCache()
        self._streams: dict[str, _Stream] = {}
        self._older_tokens: dict[str, _OlderState] = {}
        self._stream_tokens: dict[str, set[str]] = {}
        self._bus_queue: deque[dict] = deque()
        self._bus_scheduled = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._history_tasks: set[asyncio.Task] = set()
        self._follower_tasks: set[asyncio.Task] = set()
        self._ttl_task: asyncio.Task | None = None
        self._closed = False
        self.store.register_bus_listener(self._on_bus_row)
        if observer is not None:
            setter = getattr(observer, "set_trajectory_capture", None)
            if callable(setter):
                setter(self.capture_batch)

    @property
    def streams(self) -> dict[str, _Stream]:
        return self._streams

    def capture_batch(self, participant_id: str, batch: Batch) -> None:
        """Capture already-decoded live facts only for warm streams."""
        if self._closed:
            return
        stream = self._streams.get(participant_id)
        if stream is None:
            return
        try:
            self.cache.touch(participant_id)
            stream.capture_serial += 1
            captured = _CapturedBatch(stream.capture_serial, batch)
            if not stream.initialized:
                stream.pending_live.append(captured)
            else:
                self._apply_live(stream, captured, notify=True)
        except BaseException:
            logger.exception("trajectory live capture failed for %s", participant_id)

    async def snapshot(
        self, participant_token: str, *, before: str | None = None, limit: int = 200
    ) -> TrajectoryPage:
        """Return a bounded snapshot, initializing one participant lazily."""
        self._set_loop()
        limit = _validate_limit(limit, "trajectory.snapshot")
        _validate_participant_token(participant_token, "trajectory.snapshot")
        _validate_optional_cursor(before, "trajectory.snapshot")
        if before is not None and not before.startswith("o1-"):
            raise BadRequest("trajectory.snapshot parameter 'before' is not a valid older cursor")
        participant = self._resolve(participant_token, missing=True)
        if participant is None:
            return _missing_page()
        stream = self._streams.get(participant.id)
        if stream is None:
            stream = self._create_stream(participant)
            stream.cache.viewer_refs = 1
            await self._initialize(stream)
        elif not stream.initialized:
            await self._initialize(stream)
        else:
            self.cache.touch(participant.id)
            stream.cache.viewer_refs = max(1, stream.cache.viewer_refs)
        self._refresh_participant(stream)
        if before is None:
            return self._build_page(
                stream,
                limit=limit,
                source_before=stream.source_before,
                bus_before=stream.bus_before,
            )
        state = self._older_state(before)
        if state is None:
            return self._stale_page(
                stream, "the older cursor is no longer warm; request a fresh snapshot"
            )
        if state.daemon_epoch != self.daemon_epoch or state.stream_id != stream.cache.stream_id:
            return self._stale_page(stream, "the daemon stream changed; request a fresh snapshot")
        source_before, bus_before, loaded = await self._load_older(stream, state, limit=limit)
        stream.source_before = source_before
        stream.bus_before = bus_before
        if (
            state.record_before is not None
            and not loaded
            and not any(record.record_id == state.record_before for record in stream.ring.records())
        ):
            return self._stale_page(
                stream, "older records fell out of the warm cache; request a fresh snapshot"
            )
        return self._build_page(
            stream,
            limit=limit,
            record_before=state.record_before,
            source_before=source_before,
            bus_before=bus_before,
            preferred=loaded,
        )

    async def follow(
        self,
        participant_token: str,
        *,
        stream_id: str,
        after: str,
        wait: float,
        limit: int,
    ) -> TrajectoryDelta:
        """Wait for a stream change through an ordinary cancellable RPC."""
        self._set_loop()
        _validate_participant_token(participant_token, "trajectory.follow")
        _validate_bounded_string(stream_id, "stream_id", TRAJECTORY_IDENTIFIER_MAX_BYTES)
        _validate_bounded_string(after, "after", TRAJECTORY_CURSOR_MAX_BYTES)
        wait = _validate_wait(wait, "trajectory.follow")
        limit = _validate_limit(limit, "trajectory.follow")
        participant = self._resolve(participant_token, missing=False)
        assert participant is not None
        stream = self._streams.get(participant.id)
        parsed = _decode_follow_cursor(after)
        if parsed is None:
            raise BadRequest(
                "trajectory.follow parameter 'after' must be a cursor from trajectory.snapshot"
            )
        cursor_epoch, cursor_stream, sequence = parsed
        if cursor_epoch != self.daemon_epoch:
            return self._resync_delta(stream_id, "the daemon restarted; request a fresh snapshot")
        if stream is None or stream.cache.stream_id != stream_id or cursor_stream != stream_id:
            return self._resync_delta(
                stream_id,
                "the trajectory stream is not warm; request a fresh snapshot",
            )
        self.cache.touch(participant.id)
        read = stream.ring.changes_after(sequence, limit=limit)
        if read.resync_required:
            return self._resync_delta(stream_id, read.reason or "request a fresh snapshot")
        if read.changes:
            return self._delta_for_changes(
                stream, read.changes, after_sequence=sequence, limit=limit
            )
        if wait <= 0:
            return self._empty_delta(stream, sequence)
        task = asyncio.current_task()
        follower_id = id(task) if task is not None else id(stream)
        event = asyncio.Event()
        stream.followers[follower_id] = event
        stream.cache.follower_refs += 1
        if task is not None:
            self._follower_tasks.add(task)
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), timeout=wait)
        finally:
            stream.followers.pop(follower_id, None)
            stream.cache.follower_refs = max(0, stream.cache.follower_refs - 1)
            if task is not None:
                self._follower_tasks.discard(task)
        if self._streams.get(participant.id) is not stream:
            return self._resync_delta(
                stream_id, "the trajectory stream was evicted; request a fresh snapshot"
            )
        read = stream.ring.changes_after(sequence, limit=limit)
        if read.resync_required:
            return self._resync_delta(stream_id, read.reason or "request a fresh snapshot")
        if not read.changes:
            return self._empty_delta(stream, sequence)
        return self._delta_for_changes(stream, read.changes, after_sequence=sequence, limit=limit)

    def close_viewer(self, participant_token: str, stream_id: str | None = None) -> bool:
        """Release one viewer reference without relying on it for correctness."""
        if self._closed:
            return False
        _validate_participant_token(participant_token, "trajectory.close")
        if stream_id is not None:
            _validate_bounded_string(stream_id, "stream_id", TRAJECTORY_IDENTIFIER_MAX_BYTES)
        participant = self._resolve(participant_token, missing=True)
        if participant is None:
            return False
        stream = self._streams.get(participant.id)
        if stream is None or (stream_id is not None and stream.cache.stream_id != stream_id):
            return False
        stream.cache.viewer_refs = max(0, stream.cache.viewer_refs - 1)
        self.cache.touch(participant.id)
        return True

    async def aclose(self) -> None:
        """Unsubscribe, cancel trajectory tasks, and release warm state."""
        if self._closed:
            return
        self._closed = True
        self.store.unregister_bus_listener(self._on_bus_row)
        if self.observer is not None:
            setter = getattr(self.observer, "set_trajectory_capture", None)
            if callable(setter):
                setter(None)
        if self._ttl_task is not None:
            self._ttl_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ttl_task
            self._ttl_task = None
        tasks = [task for task in (*self._history_tasks, *self._follower_tasks) if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for stream in tuple(self._streams.values()):
            if stream.pending_wake is not None:
                stream.pending_wake.cancel()
            self._wake_followers(stream)
        self._streams.clear()
        self._older_tokens.clear()
        self._stream_tokens.clear()
        self._bus_queue.clear()
        for participant_id in tuple(self.cache._entries):
            self.cache.remove(participant_id)

    shutdown = aclose

    def _set_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop

    def _create_stream(self, participant: Participant) -> _Stream:
        stream_id = uuid.uuid4().hex
        cache_stream = self.cache.add(participant.id, stream_id)
        stream = _Stream(
            participant=participant,
            cache=cache_stream,
            panel_state=PanelStateInfo(
                PanelState.WAITING,
                "loading trajectory history",
                _participant_state(participant),
            ),
        )
        self._streams[participant.id] = stream
        self._stream_tokens[stream_id] = set()
        cache_stream.loading = True
        self._ensure_ttl_task()
        for evicted in self.cache.enforce(protected={participant.id}):
            self._discard_stream(evicted.participant_id, evicted)
        return stream

    def _ensure_ttl_task(self) -> None:
        if self._ttl_task is None and self._loop is not None:
            self._ttl_task = self._loop.create_task(self._ttl_loop())

    async def _ttl_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(30.0)
            for expired in self.cache.expire():
                self._discard_stream(expired.participant_id, expired)

    async def _initialize(self, stream: _Stream) -> None:
        async with stream.initialization_lock:
            if stream.initialized:
                return
            stream.cache.loading = True
            stream.watermark = stream.capture_serial
            stream.bus_watermark = self._bus_watermark()
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

    def _apply_history(self, stream: _Stream, result: HistoryLoad, *, older: bool) -> None:
        page = result.page
        if result.trusted:
            stream.trusted = True
            stream.live_allowed = is_trusted_provenance(
                result.page.provenance
            ) or is_trusted_provenance(stream.participant.session_correlation)
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
            self._add_history_failure(stream, result)

    def _set_initial_state(self, stream: _Stream, result: HistoryLoad) -> None:
        self._refresh_participant(stream)
        participant_state = _participant_state(stream.participant)
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
        stream.panel_state = PanelStateInfo(state, message, participant_state)

    async def _load_older(
        self, stream: _Stream, state: _OlderState, *, limit: int
    ) -> tuple[str | None, int | None, tuple[TrajectoryRecord, ...]]:
        source_before = state.source_before
        bus_before = state.bus_before
        marker_exists = state.record_before is not None and any(
            record.record_id == state.record_before for record in stream.ring.records()
        )
        before_count = (
            _count_records_before(stream.ring.records(), state.record_before)
            if state.record_before is not None
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
                if result.source_epoch is not None:
                    if (
                        stream.source_epoch is not None
                        and stream.source_epoch != result.source_epoch
                    ):
                        self._add_boundary(stream, stream.source_epoch, result.source_epoch)
                        self._add_gap(stream, "transcript", "transcript session rotated")
                    stream.source_epoch = result.source_epoch
                loaded_records.extend(
                    project_history_page(
                        result.page,
                        participant_id=stream.participant.id,
                        source_epoch=result.source_epoch
                        or stream.source_epoch
                        or source_epoch_for(stream.participant, None),
                    )
                )
                self._merge_records(stream, loaded_records, notify=False)
                source_before = result.page.older_cursor if result.page.has_older else None
                stream.transcript_floor = result.page.older_cursor or result.page.cursor
            else:
                self._add_history_failure(stream, result)
                source_before = None
        if bus_before is not None and needs_more:
            rows = self._bus_history(stream.participant.id, before_id=bus_before, stream=stream)
            records = [
                record
                for row in rows
                if (record := project_bus_row(row, stream.participant.id)) is not None
            ]
            loaded_records.extend(records)
            self._merge_records(stream, records, notify=False)
            bus_before = rows[0]["id"] if len(rows) >= BUS_PARTICIPANT_PAGE_MAX_LIMIT else None
            self._update_theater_floor(stream, records)
        return source_before, bus_before, tuple(loaded_records)

    def _apply_live(self, stream: _Stream, captured: _CapturedBatch, *, notify: bool) -> None:
        batch = captured.batch
        attachment = batch.attached
        if attachment is not None:
            if not is_trusted_provenance(attachment.correlation):
                stream.live_allowed = False
                self._set_panel(
                    stream, PanelState.UNTRUSTED, "live transcript identity is untrusted"
                )
                self._add_gap(stream, "transcript", "live transcript attachment is untrusted")
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
            self._add_gap(stream, "transcript", reason)
            if batch.error_code == TRANSCRIPT_IDENTITY_LOST_CODE:
                self._set_panel(
                    stream,
                    PanelState.UNTRUSTED,
                    transcript_identity_recovery_message(stream.participant.id, reason),
                )
            elif any(not record.record_id.startswith("bus:") for record in stream.ring.records()):
                self._set_panel(stream, PanelState.STALE, f"live transcript unavailable: {reason}")
            else:
                self._set_panel(
                    stream, PanelState.UNAVAILABLE, f"live transcript unavailable: {reason}"
                )
            return
        epoch = stream.source_epoch or source_epoch_for(stream.participant, None)
        records = project_batch(
            batch,
            participant_id=stream.participant.id,
            source_epoch=epoch,
        )
        self._merge_records(stream, records, notify=notify)
        if records and stream.panel_state.state in {
            PanelState.WAITING,
            PanelState.UNAVAILABLE,
            PanelState.STALE,
            PanelState.UNTRUSTED,
        }:
            self._set_panel(stream, PanelState.READY, "live transcript records are available")

    def _add_history_failure(self, stream: _Stream, result: HistoryLoad) -> None:
        reason = result.message or result.page.error or "history source failed"
        self._add_gap(stream, "transcript", reason)

    def _add_boundary(self, stream: _Stream, old: str, new: str) -> None:
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

    def _add_gap(
        self,
        stream: _Stream,
        source: str,
        reason: str,
        start: str | None = None,
        end: str | None = None,
    ) -> None:
        gap = CoverageGap(source, reason, start=start, end=end)
        if gap not in stream.gaps:
            stream.gaps.append(gap)
            del stream.gaps[64:]

    def _set_panel(self, stream: _Stream, state: PanelState, message: str) -> None:
        stream.panel_state = PanelStateInfo(state, message, _participant_state(stream.participant))

    def _refresh_participant(self, stream: _Stream) -> None:
        getter = getattr(self.registry, "get", None)
        if not callable(getter):
            return
        try:
            participant = getter(stream.participant.id)
        except NotFound:
            return
        if isinstance(participant, Participant):
            stream.participant = participant
            stream.panel_state = PanelStateInfo(
                stream.panel_state.state,
                stream.panel_state.message,
                _participant_state(participant),
            )

    def _merge_records(
        self,
        stream: _Stream,
        records: tuple[TrajectoryRecord, ...] | list[TrajectoryRecord],
        *,
        notify: bool,
    ) -> tuple[RecordChange, ...]:
        previous = {record.record_id: stream.ring.get(record.record_id) for record in records}
        changes = stream.ring.merge(records)
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
                self._wake_followers(stream)
            else:
                self._schedule_wake(stream)
        return changes

    def _schedule_wake(self, stream: _Stream) -> None:
        if self._loop is None or stream.pending_wake is not None:
            return
        stream.pending_wake = self._loop.call_later(
            TRAJECTORY_MUTABLE_UPDATE_COALESCE_MS / 1000.0,
            self._flush_wake,
            stream,
        )

    def _flush_wake(self, stream: _Stream) -> None:
        stream.pending_wake = None
        if not self._closed:
            self._wake_followers(stream)

    @staticmethod
    def _wake_followers(stream: _Stream) -> None:
        for event in tuple(stream.followers.values()):
            event.set()

    def _merge_bus_rows(self, stream: _Stream, rows: list[dict], *, notify: bool) -> None:
        records = [
            record
            for row in rows
            if (record := project_bus_row(row, stream.participant.id)) is not None
        ]
        self._merge_records(stream, records, notify=notify)
        self._update_theater_floor(stream, records)
        for row in rows:
            if row.get("kind") == "participant.dead":
                self._refresh_participant(stream)
                stream.panel_state = PanelStateInfo(
                    stream.panel_state.state,
                    stream.panel_state.message,
                    TrajectoryParticipantState.DEAD,
                )

    @staticmethod
    def _update_theater_floor(stream: _Stream, records: list[TrajectoryRecord]) -> None:
        if records:
            ids = [record.raw_index for record in records]
            floor = min(ids)
            if stream.theater_floor is not None and stream.theater_floor.startswith("bus:"):
                with contextlib.suppress(ValueError):
                    floor = min(floor, int(stream.theater_floor.removeprefix("bus:")))
            stream.theater_floor = f"bus:{floor}"

    def _on_bus_row(self, row: dict) -> None:
        if self._closed or not self._streams:
            return
        try:
            self._bus_queue.append(dict(row))
            if self._loop is not None and not self._bus_scheduled:
                self._bus_scheduled = True
                self._loop.call_soon(self._drain_bus_queue)
        except BaseException:
            logger.exception("trajectory bus capture failed")

    def _drain_bus_queue(self) -> None:
        self._bus_scheduled = False
        while self._bus_queue:
            row = self._bus_queue.popleft()
            if row.get("kind") not in ALLOWLISTED_BUS_KINDS:
                continue
            affected = {
                value for value in (row.get("from_id"), row.get("to_id")) if isinstance(value, str)
            } & self._streams.keys()
            for participant_id in affected:
                stream = self._streams.get(participant_id)
                if stream is not None:
                    try:
                        self._merge_bus_rows(stream, [row], notify=True)
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
        stream: _Stream | None = None,
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

    def _bus_watermark(self) -> int:
        try:
            rows = self.store.bus_tail(limit=1)
        except Exception:
            return 0
        return rows[-1]["id"] if rows and type(rows[-1].get("id")) is int else 0

    def _build_page(
        self,
        stream: _Stream,
        *,
        limit: int,
        record_before: str | None = None,
        source_before: str | None = None,
        bus_before: int | None = None,
        preferred: tuple[TrajectoryRecord, ...] = (),
    ) -> TrajectoryPage:
        ordered = order_records(stream.ring.records())
        if record_before is not None:
            try:
                index = next(
                    index
                    for index, record in enumerate(ordered)
                    if record.record_id == record_before
                )
            except StopIteration:
                candidates = order_records(preferred)
            else:
                candidates = order_records((*ordered[:index], *preferred))
        else:
            candidates = ordered
        tail = candidates[-limit:] if candidates else ()
        record_limited = len(candidates) > len(tail)
        state_has_older = record_limited or source_before is not None or bus_before is not None
        if not tail and record_before is not None and not state_has_older:
            state_has_older = False
        return self._fit_page(
            stream,
            tail,
            has_older=state_has_older,
            source_before=source_before,
            bus_before=bus_before,
        )

    def _fit_page(
        self,
        stream: _Stream,
        records: tuple[TrajectoryRecord, ...],
        *,
        has_older: bool,
        source_before: str | None,
        bus_before: int | None,
    ) -> TrajectoryPage:
        for count in range(len(records), -1, -1):
            selected = records[-count:] if count else ()
            byte_truncated = count < len(records)
            older = has_older or byte_truncated
            older_cursor = None
            if older:
                older_cursor = self._make_older(
                    stream,
                    source_before=source_before,
                    bus_before=bus_before,
                    record_before=selected[0].record_id if selected else None,
                )
            page = TrajectoryPage(
                panel_state=stream.panel_state,
                stream_id=stream.cache.stream_id,
                cursor=self._follow_cursor(stream, stream.ring.current_sequence),
                records=selected,
                groups=groups_for_records(selected),
                older_cursor=older_cursor,
                has_older=older,
                coverage=TrajectoryCoverage(
                    transcript_floor=stream.transcript_floor,
                    theater_floor=stream.theater_floor,
                    gaps=tuple(stream.gaps),
                ),
                truncated_by_bytes=byte_truncated,
            )
            if _wire_bytes(page.to_wire()) <= TRAJECTORY_RESPONSE_MAX_BYTES:
                return page
        raise BadRequest(
            "trajectory response envelope exceeds the 1 MiB limit; retry after requesting "
            "a fresh snapshot"
        )

    def _delta_for_changes(
        self,
        stream: _Stream,
        changes: tuple[RecordChange, ...],
        *,
        after_sequence: int,
        limit: int,
    ) -> TrajectoryDelta:
        changes = changes[:limit]
        for count in range(len(changes), -1, -1):
            selected = changes[:count]
            sequence = selected[-1].sequence if selected else after_sequence
            delta = TrajectoryDelta(
                stream_id=stream.cache.stream_id,
                cursor=self._follow_cursor(stream, sequence),
                upserts=tuple(TrajectoryUpsert(change.record) for change in selected),
            )
            if _wire_bytes(delta.to_wire()) <= TRAJECTORY_RESPONSE_MAX_BYTES:
                if not selected and changes:
                    break
                return delta
        if changes:
            return self._resync_delta(
                stream.cache.stream_id,
                "one trajectory update cannot fit the 1 MiB response limit; "
                "request a fresh snapshot",
            )
        return self._resync_delta(
            stream.cache.stream_id,
            "one trajectory update cannot fit the 1 MiB response limit; request a fresh snapshot",
        )

    def _empty_delta(self, stream: _Stream, sequence: int) -> TrajectoryDelta:
        return TrajectoryDelta(
            stream_id=stream.cache.stream_id,
            cursor=self._follow_cursor(stream, max(sequence, stream.ring.current_sequence)),
        )

    def _resync_delta(self, stream_id: str, reason: str) -> TrajectoryDelta:
        return TrajectoryDelta(stream_id=stream_id, resync_required=True, reason=reason)

    def _stale_page(self, stream: _Stream, message: str) -> TrajectoryPage:
        return TrajectoryPage(
            panel_state=PanelStateInfo(
                PanelState.STALE,
                message,
                _participant_state(stream.participant),
            ),
            stream_id=stream.cache.stream_id,
            cursor=self._follow_cursor(stream, stream.ring.current_sequence),
            coverage=TrajectoryCoverage(
                transcript_floor=stream.transcript_floor,
                theater_floor=stream.theater_floor,
                gaps=tuple(stream.gaps),
            ),
        )

    def _older_state(self, token: str) -> _OlderState | None:
        if not isinstance(token, str) or not token.startswith("o1-"):
            raise BadRequest("trajectory.snapshot parameter 'before' is not a valid older cursor")
        return self._older_tokens.get(token)

    def _make_older(
        self,
        stream: _Stream,
        *,
        source_before: str | None,
        bus_before: int | None,
        record_before: str | None,
    ) -> str:
        token = f"o1-{uuid.uuid4().hex}"
        self._older_tokens[token] = _OlderState(
            self.daemon_epoch,
            stream.cache.stream_id,
            source_before,
            bus_before,
            record_before,
        )
        tokens = self._stream_tokens.setdefault(stream.cache.stream_id, set())
        tokens.add(token)
        if len(tokens) > 256:
            old = next(iter(tokens))
            tokens.remove(old)
            self._older_tokens.pop(old, None)
        return token

    def _follow_cursor(self, stream: _Stream, sequence: int) -> str:
        return f"c1-{self.daemon_epoch}-{stream.cache.stream_id}-{sequence}"

    def _resolve(self, token: str, *, missing: bool) -> Participant | None:
        try:
            return self.registry.resolve(token)
        except NotFound:
            if missing:
                return None
            raise

    def _discard_stream(self, participant_id: str, stream: CacheStream) -> None:
        current = self._streams.get(participant_id)
        if current is not None and current.cache is stream:
            self._streams.pop(participant_id, None)
            tokens = self._stream_tokens.pop(stream.stream_id, set())
            for token in tokens:
                self._older_tokens.pop(token, None)
            self._wake_followers(current)
            if current.pending_wake is not None:
                current.pending_wake.cancel()
        self.cache.remove(participant_id)


def _decode_follow_cursor(value: str) -> tuple[str, str, int] | None:
    if not isinstance(value, str):
        return None
    parts = value.split("-")
    if len(parts) != 4 or parts[0] != "c1":
        return None
    if not parts[1] or not parts[2] or not parts[3].isdigit():
        return None
    return parts[1], parts[2], int(parts[3])


def _count_records_before(records: tuple[TrajectoryRecord, ...], marker: str) -> int:
    ordered = order_records(records)
    try:
        index = next(index for index, record in enumerate(ordered) if record.record_id == marker)
    except StopIteration:
        return 0
    return index


def _validate_participant_token(value: object, method_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise BadRequest(f"{method_name} requires non-empty string parameter 'id'")
    _validate_encoded_length(
        value, TRAJECTORY_IDENTIFIER_MAX_BYTES, f"{method_name} parameter 'id'"
    )


def _validate_bounded_string(value: object, key: str, maximum: int) -> None:
    if not isinstance(value, str) or not value:
        raise BadRequest(f"trajectory parameter {key!r} must be a non-empty string")
    _validate_encoded_length(value, maximum, f"trajectory parameter {key!r}")


def _validate_optional_cursor(value: object, method_name: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value:
        raise BadRequest(f"{method_name} parameter 'before' must be a non-empty string or null")
    _validate_encoded_length(
        value, TRAJECTORY_CURSOR_MAX_BYTES, f"{method_name} parameter 'before'"
    )


def _validate_limit(value: object, method_name: str) -> int:
    if type(value) is not int or value <= 0:
        raise BadRequest(f"{method_name} parameter 'limit' must be a positive integer")
    return min(value, TRAJECTORY_PAGE_RECORD_LIMIT)


def _validate_wait(value: object, method_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise BadRequest(f"{method_name} parameter 'wait' must be a non-negative finite number")
    return min(float(value), TRAJECTORY_FOLLOW_TIMEOUT_SECONDS)


def _validate_encoded_length(value: str, maximum: int, label: str) -> None:
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise BadRequest(f"{label} must contain valid UTF-8") from exc
    if encoded_length > maximum:
        raise BadRequest(f"{label} exceeds {maximum} encoded bytes")


def _participant_state(participant: Participant) -> TrajectoryParticipantState:
    if participant.status is Status.DEAD:
        return TrajectoryParticipantState.DEAD
    if participant.tier is Tier.EXTERNAL:
        return TrajectoryParticipantState.EXTERNAL
    return TrajectoryParticipantState.LIVE


def _missing_page() -> TrajectoryPage:
    return TrajectoryPage(
        panel_state=PanelStateInfo(
            PanelState.UNAVAILABLE,
            "participant is missing; refresh the participant tree and select an existing id",
            TrajectoryParticipantState.MISSING,
        )
    )


def _wire_bytes(value: dict[str, object]) -> int:
    return len(json.dumps(value, separators=(",", ":")).encode("utf-8"))


__all__ = ["TrajectoryService"]
