"""Public snapshot, follow, and close coordination for daemon trajectories."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import deque
from dataclasses import dataclass

from theater.constants.trajectory import (
    TRAJECTORY_CURSOR_MAX_BYTES,
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
    TRAJECTORY_OLDER_CURSOR_LIMIT,
    TRAJECTORY_PAGE_RECORD_LIMIT,
)
from theater.daemon.trajectory.cache import CacheStream, RecordChange, TrajectoryCache
from theater.daemon.trajectory.merge import order_records
from theater.daemon.trajectory.params import (
    validate_bounded_string,
    validate_identifier,
    validate_limit,
    validate_optional_cursor,
    validate_participant_token,
    validate_wait,
)
from theater.daemon.trajectory.responses import (
    TrajectoryResponseTooLarge,
    decode_follow_cursor,
    empty_delta,
    fit_delta,
    fit_page,
    missing_page,
    response_values_for_stream,
    resync_delta,
    stale_page,
)
from theater.daemon.trajectory.runtime import TrajectoryRuntime
from theater.daemon.trajectory.stream import TrajectoryStream
from theater.daemon.trajectory.theater_events import ALLOWLISTED_BUS_KINDS, project_bus_row
from theater.models import BadRequest, NotFound, Participant
from theater.trajectory import (
    PanelState,
    TrajectoryDelta,
    TrajectoryPage,
    TrajectoryParticipantState,
    TrajectoryRecord,
)
from theater.trajectory.location import TrajectoryLocation, TrajectoryLocationResolution


@dataclass(frozen=True, slots=True)
class _OlderState:
    daemon_epoch: str
    stream_id: str
    source_before: str | None
    bus_before: int | None
    record_before: str | None


class TrajectoryService:
    """Expose bounded trajectory reads over daemon-owned warm streams."""

    def __init__(self, store, registry, observer=None) -> None:
        self.store = store
        self.registry = registry
        self.observer = observer
        self.daemon_epoch = uuid.uuid4().hex
        self._runtime = TrajectoryRuntime(store, registry, observer)
        self._runtime.set_eviction_listener(self._forget_stream_tokens)
        self._older_tokens: dict[str, _OlderState] = {}
        self._stream_tokens: dict[str, deque[str]] = {}
        self._follower_tasks: set[asyncio.Task] = set()
        self._closed = False

    @property
    def streams(self) -> dict[str, TrajectoryStream]:
        return self._runtime.streams

    @property
    def cache(self) -> TrajectoryCache:
        return self._runtime.cache

    @cache.setter
    def cache(self, value: TrajectoryCache) -> None:
        self._runtime.replace_cache(value)

    def capture_batch(self, participant_id: str, batch) -> None:
        self._runtime.capture_batch(participant_id, batch)

    def locate(self, participant_token: str, record_id: str) -> TrajectoryLocation:
        validate_identifier(participant_token, "trajectory.locate", "id")
        validate_identifier(record_id, "trajectory.locate", "record_id")
        participant = self._resolve(participant_token, missing=True)
        if participant is None:
            return TrajectoryLocation(
                participant_token,
                record_id,
                TrajectoryLocationResolution.NOT_FOUND,
                message="participant is not known to Theater",
            )
        stream = self.streams.get(participant.id)
        cached = stream.ring.get(record_id) if stream is not None else None
        if cached is not None and cached.participant_id == participant.id:
            return TrajectoryLocation(
                participant.id,
                record_id,
                TrajectoryLocationResolution.EXACT,
                record=cached,
            )
        row_id = _bus_record_id(record_id)
        if row_id is None:
            return TrajectoryLocation(
                participant.id,
                record_id,
                TrajectoryLocationResolution.UNAVAILABLE,
                message="record is not warm; request trajectory.snapshot before navigating to it",
            )
        row = self.store.bus_record_for_participant(
            participant.id,
            row_id,
            kinds=ALLOWLISTED_BUS_KINDS,
        )
        record = project_bus_row(row, participant.id) if row is not None else None
        if record is None or record.record_id != record_id:
            return TrajectoryLocation(
                participant.id,
                record_id,
                TrajectoryLocationResolution.NOT_FOUND,
                message="record is not available for this participant",
            )
        return TrajectoryLocation(
            participant.id,
            record_id,
            TrajectoryLocationResolution.EXACT,
            record=record,
        )

    async def snapshot(
        self,
        participant_token: str,
        *,
        before: str | None = None,
        limit: int = TRAJECTORY_PAGE_RECORD_LIMIT,
    ) -> TrajectoryPage:
        self._runtime.set_loop()
        limit = validate_limit(limit, "trajectory.snapshot")
        validate_participant_token(participant_token, "trajectory.snapshot")
        validate_optional_cursor(before, "trajectory.snapshot")
        if before is not None and not before.startswith("o1-"):
            raise BadRequest("trajectory.snapshot parameter 'before' is not a valid older cursor")
        participant = self._resolve(participant_token, missing=True)
        if participant is None:
            return missing_page()
        stream = self.streams.get(participant.id)
        if stream is None:
            stream = self._runtime.create_stream(participant)
            stream.cache.viewer_refs = 1
            await self._runtime.initialize(stream)
        elif not stream.initialized:
            await self._runtime.initialize(stream)
        else:
            self.cache.touch(participant.id)
            stream.cache.viewer_refs = max(1, stream.cache.viewer_refs)
            if before is None and stream.panel_state.state in {
                PanelState.STALE,
                PanelState.UNAVAILABLE,
                PanelState.UNTRUSTED,
            }:
                await self._runtime.refresh(stream)
        self._runtime.refresh_participant(stream)
        if before is None:
            return self._build_page(
                stream,
                limit=limit,
                source_before=stream.source_before,
                bus_before=stream.bus_before,
            )
        state = self._older_state(before)
        if state is None:
            return stale_page(
                stream,
                daemon_epoch=self.daemon_epoch,
                message="the older cursor is no longer warm; request a fresh snapshot",
            )
        if state.daemon_epoch != self.daemon_epoch or state.stream_id != stream.cache.stream_id:
            return stale_page(
                stream,
                daemon_epoch=self.daemon_epoch,
                message="the daemon stream changed; request a fresh snapshot",
            )
        source_before, bus_before, loaded = await self._runtime.load_older(
            stream,
            source_before=state.source_before,
            bus_before=state.bus_before,
            record_before=state.record_before,
            limit=limit,
        )
        stream.source_before = source_before
        stream.bus_before = bus_before
        if (
            state.record_before is not None
            and not loaded
            and not any(record.record_id == state.record_before for record in stream.ring.records())
        ):
            return stale_page(
                stream,
                daemon_epoch=self.daemon_epoch,
                message="older records fell out of the warm cache; request a fresh snapshot",
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
        self._runtime.set_loop()
        validate_participant_token(participant_token, "trajectory.follow")
        validate_bounded_string(stream_id, "stream_id", TRAJECTORY_IDENTIFIER_MAX_BYTES)
        validate_bounded_string(after, "after", TRAJECTORY_CURSOR_MAX_BYTES)
        wait = validate_wait(wait, "trajectory.follow")
        limit = validate_limit(limit, "trajectory.follow")
        parsed = decode_follow_cursor(after)
        if parsed is None:
            raise BadRequest(
                "trajectory.follow parameter 'after' must be a cursor from trajectory.snapshot"
            )
        cursor_epoch, cursor_stream, sequence = parsed
        target = self._follow_stream(participant_token, stream_id, cursor_epoch, cursor_stream)
        if isinstance(target, TrajectoryDelta):
            return target
        stream = target
        self._runtime.refresh_participant(stream, notify=True)
        self.cache.touch(stream.participant.id)
        read = stream.ring.changes_after(sequence, limit=limit)
        if read.resync_required:
            return resync_delta(stream_id, read.reason or "request a fresh snapshot")
        if read.changes:
            return self._delta_for_changes(stream, read.changes, after_sequence=sequence)
        if _terminal_participant_state(stream):
            return self._empty_delta(stream, sequence=sequence)
        if wait <= 0:
            return self._empty_delta(stream, sequence=sequence)
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
        if self.streams.get(stream.participant.id) is not stream:
            return resync_delta(
                stream_id,
                "the trajectory stream was evicted; request a fresh snapshot",
            )
        self._runtime.refresh_participant(stream)
        read = stream.ring.changes_after(sequence, limit=limit)
        if read.resync_required:
            return resync_delta(stream_id, read.reason or "request a fresh snapshot")
        if not read.changes:
            return self._empty_delta(stream, sequence=sequence)
        return self._delta_for_changes(stream, read.changes, after_sequence=sequence)

    def close_viewer(self, participant_token: str, stream_id: str | None = None) -> bool:
        if self._closed:
            return False
        validate_participant_token(participant_token, "trajectory.close")
        if stream_id is not None:
            validate_bounded_string(stream_id, "stream_id", TRAJECTORY_IDENTIFIER_MAX_BYTES)
        participant = self._resolve(participant_token, missing=True)
        if participant is None:
            return False
        stream = self.streams.get(participant.id)
        if stream is None or (stream_id is not None and stream.cache.stream_id != stream_id):
            return False
        stream.cache.viewer_refs = max(0, stream.cache.viewer_refs - 1)
        self.cache.touch(participant.id)
        return True

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = [task for task in self._follower_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._runtime.aclose()
        self._older_tokens.clear()
        self._stream_tokens.clear()

    shutdown = aclose

    def _build_page(
        self,
        stream: TrajectoryStream,
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
        has_older = (
            len(candidates) > len(tail) or source_before is not None or bus_before is not None
        )

        def make_older(source: str | None, bus: int | None, marker: str | None) -> str:
            return self._make_older(
                stream,
                source_before=source,
                bus_before=bus,
                record_before=marker,
            )

        try:
            return fit_page(
                stream,
                tail,
                daemon_epoch=self.daemon_epoch,
                has_older=has_older,
                source_before=source_before,
                bus_before=bus_before,
                make_older=make_older,
            )
        except TrajectoryResponseTooLarge as exc:
            raise BadRequest(
                "trajectory response envelope exceeds the 1 MiB limit; request a fresh snapshot"
            ) from exc

    def _follow_stream(
        self,
        participant_token: str,
        stream_id: str,
        cursor_epoch: str,
        cursor_stream: str,
    ) -> TrajectoryStream | TrajectoryDelta:
        if cursor_epoch != self.daemon_epoch:
            return resync_delta(stream_id, "the daemon restarted; request a fresh snapshot")
        participant = self._resolve(participant_token, missing=True)
        stream = self.streams.get(participant.id if participant is not None else participant_token)
        if stream is None or stream.cache.stream_id != stream_id or cursor_stream != stream_id:
            return resync_delta(
                stream_id,
                "the trajectory stream is not warm; request a fresh snapshot",
            )
        return stream

    def _delta_for_changes(
        self,
        stream: TrajectoryStream,
        changes: tuple[RecordChange, ...],
        *,
        after_sequence: int,
    ) -> TrajectoryDelta:
        delta = fit_delta(
            stream,
            changes,
            daemon_epoch=self.daemon_epoch,
            after_sequence=after_sequence,
            response_values=response_values_for_stream(stream),
            panel_state=stream.panel_state,
        )
        if delta is not None:
            return delta
        return resync_delta(
            stream.cache.stream_id,
            "one trajectory update cannot fit the 1 MiB response limit; request a fresh snapshot",
        )

    def _empty_delta(self, stream: TrajectoryStream, *, sequence: int) -> TrajectoryDelta:
        return empty_delta(
            stream,
            daemon_epoch=self.daemon_epoch,
            sequence=sequence,
            panel_state=stream.panel_state,
        )

    def _older_state(self, token: str) -> _OlderState | None:
        if not token.startswith("o1-"):
            raise BadRequest("trajectory.snapshot parameter 'before' is not a valid older cursor")
        return self._older_tokens.get(token)

    def _make_older(
        self,
        stream: TrajectoryStream,
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
        tokens = self._stream_tokens.setdefault(stream.cache.stream_id, deque())
        tokens.append(token)
        while len(tokens) > TRAJECTORY_OLDER_CURSOR_LIMIT:
            self._older_tokens.pop(tokens.popleft(), None)
        return token

    def _forget_stream_tokens(self, stream: CacheStream) -> None:
        tokens = self._stream_tokens.pop(stream.stream_id, ())
        for token in tokens:
            self._older_tokens.pop(token, None)

    def _resolve(self, token: str, *, missing: bool) -> Participant | None:
        try:
            return self.registry.resolve(token)
        except NotFound:
            if missing:
                return None
            raise


def _bus_record_id(record_id: str) -> int | None:
    prefix = "bus:"
    if not record_id.startswith(prefix):
        return None
    value = record_id.removeprefix(prefix)
    if not value or not value.isascii() or not value.isdecimal() or value != str(int(value)):
        return None
    return int(value)


def _terminal_participant_state(stream: TrajectoryStream) -> bool:
    return stream.panel_state.participant_state in {
        TrajectoryParticipantState.DEAD,
        TrajectoryParticipantState.EXTERNAL,
        TrajectoryParticipantState.MISSING,
    }


__all__ = ["TrajectoryService"]
