"""The trajectory bus listener and stream cache: lazy listening, follow wakeups, eviction."""

from __future__ import annotations

import asyncio
import threading

import pytest

from tests.support.trajectory_service import (
    FakeBusStore,
    FakeObserver,
    FakeRegistry,
    FakeSource,
    assistant_event,
    history_page,
    snapshot_record,
    trajectory_participant,
    wire_size,
)
from theater.constants.daemon import (
    BUS_KIND_PARTICIPANT_KILL_REQUESTED,
)
from theater.constants.trajectory import TRAJECTORY_RESPONSE_MAX_BYTES
from theater.daemon.trajectory import history as history_module
from theater.daemon.trajectory.cache import (
    CacheStream,
    RecordRing,
    TrajectoryCache,
    encoded_record_bytes,
)
from theater.daemon.trajectory.runtime import TrajectoryRuntime
from theater.daemon.trajectory.service import TrajectoryService
from theater.daemon.trajectory.stream import TrajectoryStream
from theater.daemon.trajectory.theater_events import project_bus_row
from theater.harness.contracts.events import Event, EventKind
from theater.harness.contracts.source import Batch, HistoryPage, Source
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.models import Participant
from theater.trajectory import (
    PanelState,
    PanelStateInfo,
    TrajectoryKind,
    TrajectoryStatus,
)


class BlockingSource(Source):
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    async def read(self) -> Batch:
        return Batch()

    async def history_page(self, *, before: str | None = None, limit: int = 200) -> HistoryPage:
        self.started.set()
        self.release.wait()
        return HistoryPage(
            location="/tmp/p",
            events=(Event(kind=EventKind.ASSISTANT, text="history", raw_index=1),),
            cursor="cursor",
            provenance="operator",
        )


async def test_bus_listener_is_lazy_removable_and_reregisterable() -> None:
    store = FakeBusStore()
    current = trajectory_participant()
    runtime = TrajectoryRuntime(store, FakeRegistry([current]))
    runtime.set_loop()

    assert store.listeners == []
    stream = runtime.create_stream(current)
    assert store.listeners == [runtime._on_bus_row]
    runtime._register_bus_listener()
    assert store.listeners == [runtime._on_bus_row]

    runtime._discard_stream(current.id, stream.cache)
    assert store.listeners == []
    runtime.create_stream(current)
    assert store.listeners == [runtime._on_bus_row]
    await runtime.aclose()
    assert store.listeners == []


async def test_lazy_listener_keeps_the_initial_snapshot_bus_race(monkeypatch) -> None:
    current = trajectory_participant()
    store = FakeBusStore()
    source = BlockingSource()
    observer = FakeObserver()
    monkeypatch.setattr(
        history_module,
        "open_participant_source",
        lambda _observer, **_kwargs: source,
    )
    service = TrajectoryService(store, FakeRegistry([current]), observer)

    assert store.listeners == []
    task = asyncio.create_task(service.snapshot(current.id))
    await asyncio.to_thread(source.started.wait)
    assert len(store.listeners) == 1
    store.emit(
        {
            "id": 1,
            "ts": 1.0,
            "from_id": current.id,
            "to_id": "other",
            "kind": BUS_KIND_PARTICIPANT_KILL_REQUESTED,
            "payload": {},
        }
    )
    source.release.set()
    page = await task

    assert {record.record_id for record in page.records} >= {"bus:1"}
    await service.aclose()


async def test_follow_wakes_interactions_immediately_and_coalesces_mutable_updates(
    source_opener,
):
    participant = trajectory_participant("p")
    store = FakeBusStore()
    registry = FakeRegistry([participant])
    observer = FakeObserver()
    source_opener[participant.id] = FakeSource(history_page(location=None))
    service = TrajectoryService(store, registry, observer)
    page = await service.snapshot(participant.id)
    assert page.cursor is not None and page.stream_id is not None

    mutable = asyncio.create_task(
        service.follow(
            participant.id,
            stream_id=page.stream_id,
            after=page.cursor,
            wait=1,
            limit=200,
        )
    )
    await asyncio.sleep(0)
    observer.capture(
        participant.id,
        Batch(
            trajectory=(
                TrajectoryFact(
                    kind=TrajectoryKind.ASSISTANT,
                    source="rich",
                    summary="partial",
                    status=TrajectoryStatus.RUNNING,
                    raw_index=1,
                ),
            )
        ),
    )
    await asyncio.sleep(0.01)
    assert not mutable.done()
    delta = await mutable
    assert [upsert.record.summary for upsert in delta.upserts] == ["partial"]

    after = delta.cursor
    assert after is not None
    interaction = asyncio.create_task(
        service.follow(
            participant.id,
            stream_id=page.stream_id,
            after=after,
            wait=10,
            limit=200,
        )
    )
    await asyncio.sleep(0)
    store.emit(
        {
            "id": 2,
            "ts": 2.0,
            "from_id": participant.id,
            "to_id": "other",
            "kind": BUS_KIND_PARTICIPANT_KILL_REQUESTED,
            "payload": {},
        }
    )
    result = await asyncio.wait_for(interaction, timeout=5.0)
    assert [upsert.record.record_id for upsert in result.upserts] == ["bus:2"]

    cancelled = asyncio.create_task(
        service.follow(
            participant.id,
            stream_id=page.stream_id,
            after=result.cursor or after,
            wait=10,
            limit=200,
        )
    )
    await asyncio.sleep(0)
    assert service.streams[participant.id].followers
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert service.streams[participant.id].followers == {}
    assert service.streams[participant.id].cache.follower_refs == 0
    await service.aclose()


def test_ring_detects_eviction_and_keeps_revision_precedence():
    first = snapshot_record("first")
    second = snapshot_record("second")
    ring = RecordRing(max_bytes=encoded_record_bytes(second))
    ring.merge((first, second))
    assert ring.floor_sequence == 1
    assert ring.changes_after(0, limit=10).resync_required is True

    ring = RecordRing(max_bytes=10_000)
    ring.merge((snapshot_record("same", revision=1, summary="new"),))
    changes = ring.merge((snapshot_record("same", revision=0, summary="old"),))
    assert changes == ()
    assert ring.get("same").summary == "new"
    revision = ring.merge((snapshot_record("same", revision=2, summary="newest"),))[0]
    assert ring.changes_after(revision.sequence - 1, limit=10).changes == (revision,)


def test_snapshot_tail_keeps_native_history_after_bus_cache_eviction():
    native = tuple(snapshot_record(f"native-{index}") for index in range(4))
    bus = tuple(
        project_bus_row({"id": index, "kind": "agent.send", "from_id": "p", "to_id": "child"}, "p")
        for index in range(202)
    )
    ring = RecordRing(max_bytes=sum(encoded_record_bytes(record) for record in (*native, *bus[1:])))
    ring.merge((bus[0], *native, *bus[1:]))
    assert ring.floor_sequence == 1
    stream = TrajectoryStream(
        Participant(id="p", harness="test"),
        CacheStream("p", "stream", ring, 0.0),
        PanelStateInfo(PanelState.READY),
    )
    service = TrajectoryService(FakeBusStore(), FakeRegistry([stream.participant]))
    page = service._build_page(stream, limit=200)
    assert page.records[-4:] == native
    assert len(page.records) == 200
    assert page.has_older and page.older_cursor
    older = service._build_page(stream, limit=200, record_before=page.records[0].record_id)
    assert {record.record_id for record in (*older.records, *page.records)} == {
        record.record_id for record in ring.records()
    }


def test_cache_prefers_closed_streams_before_active_viewers() -> None:
    cache = TrajectoryCache(warm_streams=2)
    viewed = cache.add("viewed", "stream-viewed")
    viewed.viewer_refs = 1
    cache.add("closed", "stream-closed")
    cache.add("new", "stream-new")

    evicted = cache.enforce(protected={"new"})

    assert [entry.participant_id for entry in evicted] == ["closed"]
    assert cache.get("viewed") is viewed


async def test_snapshot_completion_and_close_refresh_idle_deadline(source_opener) -> None:
    participant = trajectory_participant("p")
    clock = [0.0]

    class AdvancingSource(FakeSource):
        async def history_page(self, *, before: str | None = None, limit: int = 200) -> HistoryPage:
            clock[0] = 10.0
            return await super().history_page(before=before, limit=limit)

    source_opener[participant.id] = AdvancingSource(history_page(location=None))
    service = TrajectoryService(FakeBusStore(), FakeRegistry([participant]), FakeObserver())
    service.cache = TrajectoryCache(clock=lambda: clock[0])

    page = await service.snapshot(participant.id)
    stream = service.streams[participant.id].cache
    assert stream.last_used == 10.0

    clock[0] = 20.0
    assert service.close_viewer(participant.id, page.stream_id)
    assert stream.last_used == 20.0
    await service.aclose()


async def test_cache_eviction_and_restart_epoch_require_resync(source_opener):
    participants = [trajectory_participant("p1"), trajectory_participant("p2")]
    store = FakeBusStore()
    registry = FakeRegistry(participants)
    observer = FakeObserver()
    for participant in participants:
        source_opener[participant.id] = FakeSource(history_page(location=None))
    service = TrajectoryService(store, registry, observer)
    service.cache = TrajectoryCache(warm_streams=1)
    first = await service.snapshot("p1")
    assert first.cursor is not None and first.stream_id is not None
    assert service.close_viewer("p1", first.stream_id) is True
    second = await service.snapshot("p2")
    assert second.stream_id is not None
    old = await service.follow(
        "p1",
        stream_id=first.stream_id,
        after=first.cursor,
        wait=0,
        limit=200,
    )
    assert old.resync_required is True
    await service.aclose()


async def test_cache_bound_evicts_unclosed_viewers(source_opener):
    participants = [trajectory_participant(f"p{index}") for index in range(3)]
    store = FakeBusStore()
    registry = FakeRegistry(participants)
    observer = FakeObserver()
    for participant in participants:
        source_opener[participant.id] = FakeSource(history_page(location=None))
    service = TrajectoryService(store, registry, observer)
    service.cache = TrajectoryCache(warm_streams=2)
    first = await service.snapshot("p0")
    await service.snapshot("p1")
    await service.snapshot("p2")
    assert service.cache.warm_count == 2
    assert "p0" not in service.streams
    result = await service.follow(
        "p0",
        stream_id=first.stream_id or "missing",
        after=first.cursor or "missing",
        wait=0,
        limit=200,
    )
    assert result.resync_required is True
    await service.aclose()


async def test_restart_epoch_returns_resync(source_opener):
    participant = trajectory_participant("p")
    store = FakeBusStore()
    registry = FakeRegistry([participant])
    observer = FakeObserver()
    source_opener[participant.id] = FakeSource(history_page(location=None))
    first_service = TrajectoryService(store, registry, observer)
    first = await first_service.snapshot("p")
    assert first.cursor is not None and first.stream_id is not None
    await first_service.aclose()

    second_service = TrajectoryService(store, registry, observer)
    await second_service.snapshot("p")
    result = await second_service.follow(
        "p",
        stream_id=first.stream_id,
        after=first.cursor,
        wait=0,
        limit=200,
    )
    assert result.resync_required is True
    await second_service.aclose()


async def test_follow_byte_cap_advances_without_skipping_updates(source_opener):
    participant = trajectory_participant("p")
    source_opener[participant.id] = FakeSource(history_page(location=None, provenance="operator"))
    store = FakeBusStore()
    registry = FakeRegistry([participant])
    observer = FakeObserver()
    service = TrajectoryService(store, registry, observer)
    page = await service.snapshot("p")
    assert page.cursor is not None and page.stream_id is not None
    observer.capture(
        participant.id,
        Batch(events=tuple(assistant_event("x" * 12_000, index) for index in range(100))),
    )

    first = await service.follow(
        "p",
        stream_id=page.stream_id,
        after=page.cursor,
        wait=0,
        limit=200,
    )

    assert 0 < len(first.upserts) < 100
    assert wire_size(first.to_wire()) <= TRAJECTORY_RESPONSE_MAX_BYTES
    assert first.cursor is not None
    second = await service.follow(
        "p",
        stream_id=page.stream_id,
        after=first.cursor,
        wait=0,
        limit=200,
    )
    assert len(first.upserts) + len(second.upserts) == 100
    await service.aclose()


async def test_listener_is_constant_time_without_viewer_and_is_removed_on_shutdown():
    participant = trajectory_participant("p")
    store = FakeBusStore()
    registry = FakeRegistry([participant])
    observer = FakeObserver()
    service = TrajectoryService(store, registry, observer)
    store.emit(
        {
            "id": 1,
            "ts": 1.0,
            "from_id": participant.id,
            "to_id": "q",
            "kind": BUS_KIND_PARTICIPANT_KILL_REQUESTED,
            "payload": {},
        }
    )
    assert service.streams == {}
    assert list(service._runtime._bus_queue) == []
    await service.aclose()
    assert store.listeners == []
    assert observer.capture is None
