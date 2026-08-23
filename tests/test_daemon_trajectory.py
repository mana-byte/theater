from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from theater.constants.daemon import (
    BUS_KIND_JOB_AWAIT_END,
    BUS_KIND_PARTICIPANT_KILL_REQUESTED,
)
from theater.constants.trajectory import TRAJECTORY_RESPONSE_MAX_BYTES
from theater.daemon.trajectory import history as history_module
from theater.daemon.trajectory.cache import RecordRing, TrajectoryCache, encoded_record_bytes
from theater.daemon.trajectory.project import project_batch, project_history_page
from theater.daemon.trajectory.service import TrajectoryService
from theater.daemon.trajectory.theater_events import project_bus_row
from theater.harness.contracts.events import Event, EventKind
from theater.harness.contracts.source import Attachment, Batch, HistoryPage, Source
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.models import NotFound, Participant, Status, Tier
from theater.trajectory import (
    LinkDirection,
    PanelState,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
)


class _Store:
    def __init__(self) -> None:
        self.listeners: list = []
        self.rows: list[dict] = []

    def register_bus_listener(self, listener) -> None:
        if listener not in self.listeners:
            self.listeners.append(listener)

    def unregister_bus_listener(self, listener) -> None:
        if listener in self.listeners:
            self.listeners.remove(listener)

    def emit(self, row: dict) -> None:
        self.rows.append(dict(row))
        for listener in tuple(self.listeners):
            listener(dict(row))

    def bus_page_for_participant(self, participant_id, *, before_id=None, limit, kinds):
        rows = [
            row
            for row in self.rows
            if participant_id in {row.get("from_id"), row.get("to_id")}
            and row.get("kind") in kinds
            and (before_id is None or row.get("id", 0) < before_id)
        ]
        return rows[-limit:]

    def bus_tail(self, limit=100, *, after_id=0):
        return [row for row in self.rows if row.get("id", 0) > after_id][-limit:]


class _Registry:
    def __init__(self, participants: list[Participant]) -> None:
        self.participants = {participant.id: participant for participant in participants}

    def resolve(self, token: str) -> Participant:
        participant = self.participants.get(token)
        if participant is None:
            raise NotFound(f"no participant {token!r}")
        return participant

    def get(self, participant_id: str) -> Participant:
        return self.resolve(participant_id)


class _Observer:
    def __init__(self) -> None:
        self.capture = None
        self.harnesses = {"fake": SimpleNamespace(observer=object())}

    def set_trajectory_capture(self, callback) -> None:
        self.capture = callback

    def transcript_identity_lost(self, _participant_id: str) -> bool:
        return False

    def history_is_ambiguous(self, _participant_id: str, _history) -> bool:
        return False


class _Source(Source):
    def __init__(self, page: HistoryPage, *, release: threading.Event | None = None) -> None:
        self.page = page
        self.release = release
        self.calls: list[str | None] = []
        self.thread_ids: list[int] = []
        self.closed = False

    async def read(self) -> Batch:
        return Batch()

    async def history_page(self, *, before: str | None = None, limit: int = 200) -> HistoryPage:
        self.calls.append(before)
        self.thread_ids.append(threading.get_ident())
        if self.release is not None and before is None:
            self.release.wait()
        return self.page

    async def aclose(self) -> None:
        self.closed = True


class _PagedSource(_Source):
    def __init__(self, pages: dict[str | None, HistoryPage]) -> None:
        super().__init__(pages[None])
        self.pages = pages

    async def history_page(self, *, before: str | None = None, limit: int = 200) -> HistoryPage:
        self.calls.append(before)
        self.thread_ids.append(threading.get_ident())
        return self.pages[before]


def _participant(
    participant_id: str,
    *,
    tier: Tier = Tier.ADOPTED,
    status: Status = Status.IDLE,
) -> Participant:
    return Participant(
        id=participant_id,
        harness="fake",
        tier=tier,
        status=status,
        cwd="/tmp/project",
        session_id=f"session-{participant_id}",
        session_correlation="operator",
        transcript_location=f"/tmp/{participant_id}.jsonl",
    )


def _event(text: str, index: int, *, offset: int | None = None) -> Event:
    return Event(
        kind=EventKind.ASSISTANT,
        text=text,
        raw_index=index,
        source_offset=offset,
    )


def _page(*events: Event, provenance: str = "operator", location: str | None = "/tmp/p"):
    return HistoryPage(
        location=location,
        events=events,
        cursor="cursor",
        provenance=provenance,
    )


def _wire_size(value: dict) -> int:
    return len(json.dumps(value, separators=(",", ":")).encode("utf-8"))


@pytest.fixture
def source_opener(monkeypatch):
    sources: dict[str, _Source] = {}

    def open_source(_observer, **kwargs):
        return sources[kwargs["participant_id"]]

    monkeypatch.setattr(history_module, "open_participant_source", open_source)
    return sources


async def test_snapshot_is_lazy_and_merges_race_captures(source_opener):
    participant = _participant("p")
    store = _Store()
    registry = _Registry([participant])
    observer = _Observer()
    release = threading.Event()
    source = _Source(_page(_event("history", 1, offset=10)), release=release)
    source_opener[participant.id] = source
    service = TrajectoryService(store, registry, observer)

    assert service.streams == {}
    assert source.calls == []
    task = asyncio.create_task(service.snapshot(participant.id, limit=20))
    while not source.calls:
        await asyncio.sleep(0)
    assert observer.capture is not None
    assert source.thread_ids[0] != threading.get_ident()

    observer.capture(participant.id, Batch(events=(_event("live", 2, offset=20),)))
    store.emit(
        {
            "id": 1,
            "ts": 1.0,
            "from_id": participant.id,
            "to_id": "other",
            "kind": BUS_KIND_PARTICIPANT_KILL_REQUESTED,
            "payload": {},
        }
    )
    release.set()
    page = await task
    record_ids = {record.record_id for record in page.records}
    assert len(record_ids) == len(page.records)
    assert any(record.summary == "history" for record in page.records)
    assert any(record.summary == "live" for record in page.records)
    assert "bus:1" in record_ids
    assert source.closed is True
    await service.aclose()


async def test_snapshot_paginates_older_history_without_gaps(source_opener):
    participant = _participant("p")
    source = _PagedSource(
        {
            None: HistoryPage(
                location="/tmp/p",
                events=(_event("four", 4), _event("five", 5)),
                cursor="live",
                older_cursor="before-four",
                has_older=True,
                provenance="operator",
            ),
            "before-four": HistoryPage(
                location="/tmp/p",
                events=(_event("two", 2), _event("three", 3)),
                cursor="live",
                older_cursor="before-two",
                has_older=True,
                provenance="operator",
            ),
            "before-two": HistoryPage(
                location="/tmp/p",
                events=(_event("zero", 0), _event("one", 1)),
                cursor="live",
                provenance="operator",
            ),
        }
    )
    source_opener[participant.id] = source
    service = TrajectoryService(_Store(), _Registry([participant]), _Observer())

    newest = await service.snapshot(participant.id, limit=2)
    middle = await service.snapshot(participant.id, before=newest.older_cursor, limit=2)
    oldest = await service.snapshot(participant.id, before=middle.older_cursor, limit=2)

    assert [[record.summary for record in page.records] for page in (newest, middle, oldest)] == [
        ["four", "five"],
        ["two", "three"],
        ["zero", "one"],
    ]
    assert newest.has_older is middle.has_older is True
    assert oldest.has_older is False
    assert source.calls == [None, "before-four", "before-two"]
    await service.aclose()


async def test_live_records_require_trusted_identity_and_recover_after_trusted_attach(
    source_opener,
):
    participant = _participant("p")
    participant.session_correlation = "heuristic"
    source_opener[participant.id] = _Source(
        _page(_event("untrusted history", 0), provenance="heuristic")
    )
    observer = _Observer()
    service = TrajectoryService(_Store(), _Registry([participant]), observer)

    initial = await service.snapshot(participant.id)
    assert initial.panel_state.state is PanelState.UNTRUSTED
    observer.capture(participant.id, Batch(events=(_event("ignored", 1),)))
    still_untrusted = await service.snapshot(participant.id)
    assert [record.summary for record in still_untrusted.records] == []

    observer.capture(
        participant.id,
        Batch(
            events=(_event("trusted live", 2),),
            attached=Attachment("/tmp/p", correlation="operator"),
        ),
    )
    recovered = await service.snapshot(participant.id)
    assert recovered.panel_state.state is PanelState.READY
    assert [record.summary for record in recovered.records] == ["trusted live"]
    await service.aclose()


def test_rich_facts_replace_matching_baseline_events():
    batch = Batch(
        events=(_event("baseline", 3, offset=44),),
        trajectory=(
            TrajectoryFact(
                kind=TrajectoryKind.ASSISTANT,
                source="rich",
                summary="rich",
                native_id="native-3",
                raw_index=3,
                source_offset=44,
            ),
        ),
    )
    records = project_batch(batch, participant_id="p", source_epoch="epoch")
    assert len(records) == 1
    assert records[0].summary == "rich"
    assert records[0].native_id == "epoch:native-3"


def test_explicit_trajectory_event_selection_preserves_control_only() -> None:
    event = _event("control", 3)
    batch = Batch(events=(event,), trajectory_events=())
    page = HistoryPage(events=(event,), trajectory_events=())

    assert project_batch(batch, participant_id="p", source_epoch="epoch") == ()
    assert project_history_page(page, participant_id="p", source_epoch="epoch") == ()


async def test_follow_wakes_interactions_immediately_and_coalesces_mutable_updates(
    source_opener,
):
    participant = _participant("p")
    store = _Store()
    registry = _Registry([participant])
    observer = _Observer()
    source_opener[participant.id] = _Source(_page(location=None))
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
            wait=1,
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
    result = await asyncio.wait_for(interaction, timeout=0.2)
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


def _record(record_id: str, *, revision: int = 0, summary: str = "x") -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=revision,
        participant_id="p",
        source_epoch="epoch",
        lane=TrajectoryLane.MODEL,
        kind=TrajectoryKind.ASSISTANT,
        source="test",
        summary=summary,
        status=TrajectoryStatus.COMPLETED,
    )


def test_ring_detects_eviction_and_keeps_revision_precedence():
    first = _record("first")
    second = _record("second")
    ring = RecordRing(max_bytes=encoded_record_bytes(second))
    ring.merge((first, second))
    assert ring.floor_sequence == 1
    assert ring.changes_after(0, limit=10).resync_required is True

    ring = RecordRing(max_bytes=10_000)
    ring.merge((_record("same", revision=1, summary="new"),))
    changes = ring.merge((_record("same", revision=0, summary="old"),))
    assert changes == ()
    assert ring.get("same").summary == "new"
    revision = ring.merge((_record("same", revision=2, summary="newest"),))[0]
    assert ring.changes_after(revision.sequence - 1, limit=10).changes == (revision,)


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
    participant = _participant("p")
    clock = [0.0]

    class AdvancingSource(_Source):
        async def history_page(self, *, before: str | None = None, limit: int = 200) -> HistoryPage:
            clock[0] = 10.0
            return await super().history_page(before=before, limit=limit)

    source_opener[participant.id] = AdvancingSource(_page(location=None))
    service = TrajectoryService(_Store(), _Registry([participant]), _Observer())
    service.cache = TrajectoryCache(clock=lambda: clock[0])

    page = await service.snapshot(participant.id)
    stream = service.streams[participant.id].cache
    assert stream.last_used == 10.0

    clock[0] = 20.0
    assert service.close_viewer(participant.id, page.stream_id)
    assert stream.last_used == 20.0
    await service.aclose()


async def test_cache_eviction_and_restart_epoch_require_resync(source_opener):
    participants = [_participant("p1"), _participant("p2")]
    store = _Store()
    registry = _Registry(participants)
    observer = _Observer()
    for participant in participants:
        source_opener[participant.id] = _Source(_page(location=None))
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
    participants = [_participant(f"p{index}") for index in range(3)]
    store = _Store()
    registry = _Registry(participants)
    observer = _Observer()
    for participant in participants:
        source_opener[participant.id] = _Source(_page(location=None))
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
    participant = _participant("p")
    store = _Store()
    registry = _Registry([participant])
    observer = _Observer()
    source_opener[participant.id] = _Source(_page(location=None))
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


@pytest.mark.parametrize(
    ("status", "tier", "provenance", "expected_panel", "expected_participant"),
    [
        (Status.IDLE, Tier.ADOPTED, "heuristic", PanelState.UNTRUSTED, "live"),
        (Status.DEAD, Tier.ADOPTED, "operator", PanelState.READY, "dead"),
        (Status.IDLE, Tier.EXTERNAL, "operator", PanelState.READY, "external"),
    ],
)
async def test_snapshot_exposes_trust_liveness_and_external_states(
    source_opener, status, tier, provenance, expected_panel, expected_participant
):
    participant = _participant("p", tier=tier, status=status)
    store = _Store()
    registry = _Registry([participant])
    observer = _Observer()
    source_opener[participant.id] = _Source(_page(_event("history", 1), provenance=provenance))
    service = TrajectoryService(store, registry, observer)
    page = await service.snapshot("p")
    assert page.panel_state.state is expected_panel
    assert page.panel_state.participant_state.value == expected_participant
    await service.aclose()


async def test_snapshot_missing_and_unavailable_are_distinct(source_opener):
    participant = _participant("p")
    store = _Store()
    registry = _Registry([participant])
    observer = _Observer()
    source_opener[participant.id] = _Source(_page(location=None, provenance="operator"))
    service = TrajectoryService(store, registry, observer)
    missing = await service.snapshot("ghost")
    assert missing.panel_state.participant_state.value == "missing"
    observer.harnesses.clear()
    unavailable = await service.snapshot("p")
    assert unavailable.panel_state.state is PanelState.UNAVAILABLE
    assert unavailable.panel_state.participant_state.value == "live"
    await service.aclose()


def test_bus_projection_is_allowlisted_and_directional():
    row = {
        "id": 7,
        "ts": 3.0,
        "from_id": "p",
        "to_id": "q",
        "kind": BUS_KIND_PARTICIPANT_KILL_REQUESTED,
        "payload": {},
    }
    left = project_bus_row(row, "p")
    right = project_bus_row(row, "q")
    assert left is not None and right is not None
    assert left.record_id == right.record_id == "bus:7"
    assert left.links[0].direction is LinkDirection.OUTGOING
    assert right.links[0].direction is LinkDirection.INCOMING
    assert project_bus_row({**row, "kind": "participant.status"}, "p") is None
    assert project_bus_row({**row, "kind": "agent.transcript"}, "p") is None


def test_await_end_timing_uses_elapsed_start() -> None:
    record = project_bus_row(
        {
            "id": 8,
            "ts": 10.0,
            "from_id": "p",
            "to_id": "q",
            "kind": BUS_KIND_JOB_AWAIT_END,
            "payload": {"state": "completed", "elapsed_seconds": 2.5},
        },
        "p",
    )

    assert record is not None and record.timing is not None
    assert record.timing.start == 7.5
    assert record.timing.end == 10.0
    assert record.timing.duration_ms == 2500.0


async def test_snapshot_response_byte_cap_wins(source_opener):
    participant = _participant("p")
    events = tuple(_event("x" * 12_000, index) for index in range(200))
    source_opener[participant.id] = _Source(_page(*events))
    store = _Store()
    registry = _Registry([participant])
    observer = _Observer()
    service = TrajectoryService(store, registry, observer)
    page = await service.snapshot("p", limit=200)
    wire = page.to_wire()
    assert _wire_size(wire) <= TRAJECTORY_RESPONSE_MAX_BYTES
    assert page.truncated_by_bytes is True
    assert len(page.records) < 200
    assert len(service._older_tokens) == 1
    await service.aclose()


async def test_follow_byte_cap_advances_without_skipping_updates(source_opener):
    participant = _participant("p")
    source_opener[participant.id] = _Source(_page(location=None, provenance="operator"))
    store = _Store()
    registry = _Registry([participant])
    observer = _Observer()
    service = TrajectoryService(store, registry, observer)
    page = await service.snapshot("p")
    assert page.cursor is not None and page.stream_id is not None
    observer.capture(
        participant.id,
        Batch(events=tuple(_event("x" * 12_000, index) for index in range(100))),
    )

    first = await service.follow(
        "p",
        stream_id=page.stream_id,
        after=page.cursor,
        wait=0,
        limit=200,
    )

    assert 0 < len(first.upserts) < 100
    assert _wire_size(first.to_wire()) <= TRAJECTORY_RESPONSE_MAX_BYTES
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
    participant = _participant("p")
    store = _Store()
    registry = _Registry([participant])
    observer = _Observer()
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
