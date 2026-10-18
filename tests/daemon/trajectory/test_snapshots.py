"""Trajectory snapshots: lazy capture, pagination, search, trust states and byte caps."""

from __future__ import annotations

import asyncio
import threading

import pytest

from tests.support.trajectory_service import (
    FakeBusStore,
    FakeObserver,
    FakePagedSource,
    FakeRegistry,
    FakeSource,
    assistant_event,
    history_page,
    trajectory_participant,
    wire_size,
)
from theater.constants.daemon import (
    BUS_KIND_PARTICIPANT_KILL_REQUESTED,
)
from theater.constants.trajectory import TRAJECTORY_RESPONSE_MAX_BYTES
from theater.daemon.trajectory.project import (
    project_batch,
    project_events_and_facts,
    project_history_page,
)
from theater.daemon.trajectory.service import TrajectoryService
from theater.harness.contracts.events import Event, EventKind, EventPath
from theater.harness.contracts.source import Attachment, Batch, HistoryPage
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.models import Status, Tier
from theater.trajectory import (
    ContentFormat,
    DetailField,
    PanelState,
    TrajectoryKind,
    TrajectorySearchResult,
)


def test_rich_projection_inherits_paths_from_its_replaced_baseline_event() -> None:
    records = project_events_and_facts(
        (
            Event(
                EventKind.TOOL_CALL,
                tool_name="read_file",
                raw_index=4,
                paths=(EventPath("src/app.py", "read"),),
            ),
        ),
        (
            TrajectoryFact(
                TrajectoryKind.TOOL_CALL,
                summary="read_file",
                raw_index=4,
                details=(
                    DetailField.from_text(
                        "arguments", '{"file_path":"src/app.py"}', format=ContentFormat.JSON
                    ),
                ),
            ),
        ),
        participant_id="p",
        source_epoch="epoch",
    )

    assert len(records) == 1
    assert any(
        detail.format is ContentFormat.PATH and detail.preview.text == "src/app.py"
        for detail in records[0].details
    )


async def test_snapshot_is_lazy_and_merges_race_captures(source_opener):
    participant = trajectory_participant("p")
    store = FakeBusStore()
    registry = FakeRegistry([participant])
    observer = FakeObserver()
    release = threading.Event()
    source = FakeSource(history_page(assistant_event("history", 1, offset=10)), release=release)
    source_opener[participant.id] = source
    service = TrajectoryService(store, registry, observer)

    assert service.streams == {}
    assert source.calls == []
    task = asyncio.create_task(service.snapshot(participant.id, limit=20))
    while not source.calls:
        await asyncio.sleep(0)
    assert observer.capture is not None
    assert source.thread_ids[0] != threading.get_ident()

    observer.capture(participant.id, Batch(events=(assistant_event("live", 2, offset=20),)))
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
    participant = trajectory_participant("p")
    source = FakePagedSource(
        {
            None: HistoryPage(
                location="/tmp/p",
                events=(assistant_event("four", 4), assistant_event("five", 5)),
                cursor="live",
                older_cursor="before-four",
                has_older=True,
                provenance="operator",
            ),
            "before-four": HistoryPage(
                location="/tmp/p",
                events=(assistant_event("two", 2), assistant_event("three", 3)),
                cursor="live",
                older_cursor="before-two",
                has_older=True,
                provenance="operator",
            ),
            "before-two": HistoryPage(
                location="/tmp/p",
                events=(assistant_event("zero", 0), assistant_event("one", 1)),
                cursor="live",
                provenance="operator",
            ),
        }
    )
    source_opener[participant.id] = source
    service = TrajectoryService(FakeBusStore(), FakeRegistry([participant]), FakeObserver())

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


async def test_search_scans_full_history_without_warming_stream(source_opener):
    participant = trajectory_participant("p")
    source = FakePagedSource(
        {
            None: HistoryPage(
                location="/tmp/p",
                events=(assistant_event("recent", 20),),
                cursor="live",
                older_cursor="older",
                has_older=True,
                provenance="operator",
            ),
            "older": HistoryPage(
                location="/tmp/p",
                trajectory=(
                    TrajectoryFact(
                        TrajectoryKind.TOOL_CALL,
                        summary="query metrics",
                        native_id="grafana-call",
                        raw_index=3,
                        mcp_server="grafana",
                        mcp_tool="query_prometheus",
                    ),
                ),
                trajectory_events=(),
                cursor="live",
                provenance="operator",
            ),
        }
    )
    source_opener[participant.id] = source
    service = TrajectoryService(FakeBusStore(), FakeRegistry([participant]), FakeObserver())

    result = await service.search(participant.id, query="grafna", limit=20)

    assert [record.mcp_server for record in result.records] == ["grafana"]
    assert result.complete is True
    assert result.scanned_records == 2
    assert source.calls == [None, "older"]
    assert service.streams == {}
    await service.aclose()


async def test_independent_searches_do_not_cancel_each_other_and_shutdown_drains_them(monkeypatch):
    from theater.daemon.trajectory import service as service_module

    participant = trajectory_participant("p")
    service = TrajectoryService(FakeBusStore(), FakeRegistry([participant]), FakeObserver())
    started = asyncio.Queue()
    release = asyncio.Event()

    async def search(_runtime, trajectory_participant, *, query, limit):
        await started.put(query)
        await release.wait()
        return TrajectorySearchResult(query=query, complete=True)

    monkeypatch.setattr(service_module, "search_history", search)
    first = asyncio.create_task(service.search(participant.id, query="first"))
    assert await started.get() == "first"
    second = asyncio.create_task(service.search(participant.id, query="second"))
    assert await started.get() == "second"
    assert not first.done()
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    release.set()
    assert (await first).query == "first"
    assert not service._search_tasks

    release.clear()
    pending = asyncio.create_task(service.search(participant.id, query="shutdown"))
    assert await started.get() == "shutdown"
    await service.aclose()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not service._search_tasks


async def test_live_records_require_trusted_identity_and_recover_after_trusted_attach(
    source_opener,
):
    participant = trajectory_participant("p")
    participant.session_correlation = "heuristic"
    source_opener[participant.id] = FakeSource(
        history_page(assistant_event("untrusted history", 0), provenance="heuristic")
    )
    observer = FakeObserver()
    service = TrajectoryService(FakeBusStore(), FakeRegistry([participant]), observer)

    initial = await service.snapshot(participant.id)
    assert initial.panel_state.state is PanelState.UNTRUSTED
    observer.capture(participant.id, Batch(events=(assistant_event("ignored", 1),)))
    still_untrusted = await service.snapshot(participant.id)
    assert [record.summary for record in still_untrusted.records] == []

    observer.capture(
        participant.id,
        Batch(
            events=(assistant_event("trusted live", 2),),
            attached=Attachment("/tmp/p", correlation="operator"),
        ),
    )
    recovered = await service.snapshot(participant.id)
    assert recovered.panel_state.state is PanelState.READY
    assert [record.summary for record in recovered.records] == ["trusted live"]
    await service.aclose()


def test_rich_facts_replace_matching_baseline_events():
    batch = Batch(
        events=(assistant_event("baseline", 3, offset=44),),
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
    event = assistant_event("control", 3)
    batch = Batch(events=(event,), trajectory_events=())
    page = HistoryPage(events=(event,), trajectory_events=())

    assert project_batch(batch, participant_id="p", source_epoch="epoch") == ()
    assert project_history_page(page, participant_id="p", source_epoch="epoch") == ()


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
    participant = trajectory_participant("p", tier=tier, status=status)
    store = FakeBusStore()
    registry = FakeRegistry([participant])
    observer = FakeObserver()
    source_opener[participant.id] = FakeSource(
        history_page(assistant_event("history", 1), provenance=provenance)
    )
    service = TrajectoryService(store, registry, observer)
    page = await service.snapshot("p")
    assert page.panel_state.state is expected_panel
    assert page.panel_state.participant_state.value == expected_participant
    await service.aclose()


async def test_snapshot_missing_and_unavailable_are_distinct(source_opener):
    participant = trajectory_participant("p")
    store = FakeBusStore()
    registry = FakeRegistry([participant])
    observer = FakeObserver()
    source_opener[participant.id] = FakeSource(history_page(location=None, provenance="operator"))
    service = TrajectoryService(store, registry, observer)
    missing = await service.snapshot("ghost")
    assert missing.panel_state.participant_state.value == "missing"
    observer.harnesses.clear()
    unavailable = await service.snapshot("p")
    assert unavailable.panel_state.state is PanelState.UNAVAILABLE
    assert unavailable.panel_state.participant_state.value == "live"
    await service.aclose()


async def test_snapshot_response_byte_cap_wins(source_opener):
    participant = trajectory_participant("p")
    events = tuple(assistant_event("x" * 12_000, index) for index in range(200))
    source_opener[participant.id] = FakeSource(history_page(*events))
    store = FakeBusStore()
    registry = FakeRegistry([participant])
    observer = FakeObserver()
    service = TrajectoryService(store, registry, observer)
    page = await service.snapshot("p", limit=200)
    wire = page.to_wire()
    assert wire_size(wire) <= TRAJECTORY_RESPONSE_MAX_BYTES
    assert page.truncated_by_bytes is True
    assert len(page.records) < 200
    assert len(service._older_tokens) == 1
    await service.aclose()
