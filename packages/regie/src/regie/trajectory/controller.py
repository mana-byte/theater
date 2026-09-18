"""Public trajectory snapshot and follow state for Régie's presentation."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from theater.frontend import FrontendClient, FrontendTransportError


@dataclass(frozen=True, slots=True)
class TrajectoryState:
    """One disposable trajectory viewer, kept separate from orchestration state."""

    participant_id: str
    stream_id: str | None
    cursor: str | None
    snapshot: Mapping[str, object]
    records: Mapping[str, Mapping[str, object]]
    stale: bool = False
    reason: str | None = None


class TrajectoryController:
    """Maintain one public trajectory projection without mixing it into state.follow."""

    def __init__(self, client: FrontendClient, *, page_size: int) -> None:
        self._client = client
        self._page_size = page_size
        self._state: TrajectoryState | None = None
        self._resnapshot_before_follow = False

    @property
    def state(self) -> TrajectoryState | None:
        return self._state

    async def open(self, participant_id: str, *, before: str | None = None) -> TrajectoryState:
        """Install one full public trajectory page only after it is internally coherent."""
        response = await self._client.trajectory.snapshot(
            participant_id,
            **({"before": before} if before is not None else {}),
            limit=self._page_size,
        )
        state = _snapshot_state(participant_id, response.value)
        self._state = state
        self._resnapshot_before_follow = False
        return state

    async def follow_once(self) -> Mapping[str, object] | None:
        """Apply one trajectory delta, or atomically rebuild after a stream resync signal."""
        state = self._state
        if state is None:
            return None
        if self._resnapshot_before_follow:
            await self.open(state.participant_id)
            return None
        if state.stream_id is None or state.cursor is None:
            return None
        try:
            response = await self._client.trajectory.follow(
                state.stream_id,
                state.cursor,
                wait_seconds=0,
            )
            delta = _mapping(response.value, "trajectory follow response")
            if delta.get("stream_id") != state.stream_id:
                raise ValueError("trajectory follow response belongs to a different stream")
            if delta.get("resync_required") is True:
                self._state = _stale(state, _optional_string(delta.get("reason")))
                self._resnapshot_before_follow = True
                await self.open(state.participant_id)
            else:
                self._state = _apply_delta(state, delta)
        except (FrontendTransportError, asyncio.CancelledError):
            self._state = _stale(state, "trajectory connection was interrupted")
            self._resnapshot_before_follow = True
            raise
        return delta

    async def load_older(self) -> TrajectoryState | None:
        """Merge one bounded historical page without disturbing the current follow cursor."""
        state = self._state
        if state is None:
            return None
        older = _optional_string(state.snapshot.get("older_cursor"))
        if older is None:
            return None
        response = await self._client.trajectory.snapshot(
            state.participant_id,
            before=older,
            limit=self._page_size,
        )
        page = _snapshot_state(state.participant_id, response.value)
        if state.stream_id is not None and page.stream_id not in {None, state.stream_id}:
            raise ValueError("older trajectory page belongs to a different stream")
        records = dict(state.records)
        _merge_records(records, page.records.values())
        snapshot = dict(state.snapshot)
        snapshot.update(page.snapshot)
        snapshot["stream_id"] = state.stream_id
        snapshot["cursor"] = state.cursor
        snapshot["records"] = [dict(record) for record in records.values()]
        self._state = TrajectoryState(
            participant_id=state.participant_id,
            stream_id=state.stream_id,
            cursor=state.cursor,
            snapshot=MappingProxyType(snapshot),
            records=MappingProxyType(records),
        )
        return self._state

    async def locate(self, record_id: str) -> Mapping[str, object] | None:
        """Resolve one record through the independent public trajectory resource."""
        state = self._state
        if state is None:
            return None
        return _mapping(
            (await self._client.trajectory.locate(state.participant_id, record_id)).value,
            "trajectory location response",
        )

    async def search(self, query: str) -> tuple[Mapping[str, object], ...]:
        """Search trajectory history through its public bounded query rather than local files."""
        state = self._state
        if state is None:
            return ()
        page = await self._client.trajectory.search(
            state.participant_id,
            query,
            limit=self._page_size,
        )
        return tuple(_mapping(item, "trajectory search item") for item in page.value.items)

    async def close(self) -> None:
        state = self._state
        self._state = None
        self._resnapshot_before_follow = False
        if state is not None and state.stream_id is not None:
            await self._client.trajectory.close(state.stream_id)


def _snapshot_state(participant_id: str, value: object) -> TrajectoryState:
    snapshot = _mapping(value, "trajectory snapshot response")
    stream_id = _optional_string(snapshot.get("stream_id"))
    cursor = _optional_string(snapshot.get("cursor"))
    records = _records_for(participant_id, snapshot.get("records", ()))
    return TrajectoryState(
        participant_id=participant_id,
        stream_id=stream_id,
        cursor=cursor,
        snapshot=MappingProxyType(dict(snapshot)),
        records=MappingProxyType(records),
    )


def _apply_delta(state: TrajectoryState, delta: Mapping[str, object]) -> TrajectoryState:
    records = dict(state.records)
    upserts = delta.get("upserts", ())
    if not isinstance(upserts, (list, tuple)):
        raise TypeError("trajectory follow upserts must be an array")
    incoming: list[Mapping[str, object]] = []
    for upsert in upserts:
        item = _mapping(upsert, "trajectory follow upsert")
        incoming.append(_record_for(state.participant_id, item.get("record")))
    _merge_records(records, incoming)
    snapshot = dict(state.snapshot)
    for name in ("panel_state", "capabilities", "overview"):
        if delta.get(name) is not None:
            snapshot[name] = delta[name]
    cursor = state.cursor
    if "cursor" in delta and delta["cursor"] is not None:
        cursor = _optional_string(delta["cursor"])
    snapshot["cursor"] = cursor
    snapshot["records"] = [dict(record) for record in records.values()]
    return TrajectoryState(
        participant_id=state.participant_id,
        stream_id=state.stream_id,
        cursor=cursor,
        snapshot=MappingProxyType(snapshot),
        records=MappingProxyType(records),
    )


def _records_for(participant_id: str, value: object) -> dict[str, Mapping[str, object]]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("trajectory snapshot records must be an array")
    records: dict[str, Mapping[str, object]] = {}
    _merge_records(records, (_record_for(participant_id, item) for item in value))
    return records


def _record_for(participant_id: str, value: object) -> Mapping[str, object]:
    record = _mapping(value, "trajectory record")
    record_id = record.get("record_id")
    if not isinstance(record_id, str) or not record_id:
        raise TypeError("trajectory record lacks a non-empty record_id")
    source_participant = record.get("participant_id")
    if source_participant is not None and source_participant != participant_id:
        raise ValueError("trajectory response contains a record for another participant")
    return MappingProxyType(dict(record))


def _merge_records(
    target: dict[str, Mapping[str, object]], records: Iterable[Mapping[str, object]]
) -> None:
    for record in records:
        assert isinstance(record, Mapping)
        record_id = record["record_id"]
        assert isinstance(record_id, str)
        previous = target.get(record_id)
        if previous is not None and _revision(record) <= _revision(previous):
            continue
        target[record_id] = record


def _revision(record: Mapping[str, object]) -> int:
    value = record.get("revision")
    return value if type(value) is int and value >= 0 else 0


def _stale(state: TrajectoryState, reason: str | None) -> TrajectoryState:
    return TrajectoryState(
        participant_id=state.participant_id,
        stream_id=state.stream_id,
        cursor=state.cursor,
        snapshot=state.snapshot,
        records=state.records,
        stale=True,
        reason=reason,
    )


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{label} must be an object with string keys")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("trajectory cursor and stream values must be strings or null")
    return value


__all__ = ["TrajectoryController", "TrajectoryState"]
