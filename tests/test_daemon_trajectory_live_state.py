from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from theater.daemon.trajectory import history as history_module
from theater.daemon.trajectory.service import TrajectoryService
from theater.harness.contracts.events import Event, EventKind
from theater.harness.contracts.source import Batch, HistoryPage, Source
from theater.models import NotFound, Participant, Status, Tier
from theater.trajectory import (
    PanelState,
    PanelStateInfo,
    TrajectoryDelta,
    TrajectoryParticipantState,
    TrajectoryValidationError,
)


class Store:
    def __init__(self) -> None:
        self.listeners: list = []
        self.rows: list[dict] = []

    def register_bus_listener(self, listener) -> None:
        if listener not in self.listeners:
            self.listeners.append(listener)

    def unregister_bus_listener(self, listener) -> None:
        if listener in self.listeners:
            self.listeners.remove(listener)

    def bus_page_for_participant(self, participant_id, *, before_id=None, limit, kinds):
        return []


class Registry:
    def __init__(self, participant: Participant) -> None:
        self.participants = {participant.id: participant}

    def resolve(self, token: str) -> Participant:
        return self.get(token)

    def get(self, participant_id: str) -> Participant:
        participant = self.participants.get(participant_id)
        if participant is None:
            raise NotFound(f"no participant {participant_id!r}")
        return participant


class Observer:
    def __init__(self) -> None:
        self.capture = None
        self.harnesses = {"fake": SimpleNamespace(observer=object())}

    def set_trajectory_capture(self, callback) -> None:
        self.capture = callback

    def transcript_identity_lost(self, _participant_id: str) -> bool:
        return False

    def history_is_ambiguous(self, _participant_id: str, _history) -> bool:
        return False


class SourcePage(Source):
    def __init__(self, pages: dict[str | None, HistoryPage]) -> None:
        self.pages = pages
        self.calls: list[str | None] = []

    async def read(self) -> Batch:
        return Batch()

    async def history_page(self, *, before: str | None = None, limit: int = 200) -> HistoryPage:
        self.calls.append(before)
        return self.pages[before]


def participant() -> Participant:
    return Participant(
        id="p",
        harness="fake",
        tier=Tier.ADOPTED,
        status=Status.IDLE,
        cwd="/tmp/project",
        session_id="session-p",
        session_correlation="operator",
        transcript_location="/tmp/p.jsonl",
    )


def page(*events: Event, provenance: str = "operator", **kwargs) -> HistoryPage:
    return HistoryPage(
        location=kwargs.pop("location", "/tmp/p"),
        events=events,
        cursor="cursor",
        provenance=provenance,
        **kwargs,
    )


def event(text: str, index: int) -> Event:
    return Event(kind=EventKind.ASSISTANT, text=text, raw_index=index)


@pytest.fixture
def source_opener(monkeypatch):
    sources: dict[str, SourcePage] = {}

    def open_source(_observer, **kwargs):
        return sources[kwargs["participant_id"]]

    monkeypatch.setattr(history_module, "open_participant_source", open_source)
    return sources


def test_delta_panel_state_wire_is_optional_and_validated() -> None:
    old = TrajectoryDelta.from_wire({"stream_id": "stream"})
    assert old == TrajectoryDelta(stream_id="stream")

    panel = PanelStateInfo(PanelState.STALE, "retry the snapshot")
    delta = TrajectoryDelta(stream_id="stream", panel_state=panel)
    assert TrajectoryDelta.from_wire(delta.to_wire()) == delta

    with pytest.raises(TrajectoryValidationError):
        TrajectoryDelta.from_wire({"stream_id": "stream", "panel_state": {"state": "bad"}})


async def test_follow_reports_waiting_to_ready_and_state_only_stale_delta(source_opener) -> None:
    current = participant()
    source_opener[current.id] = SourcePage({None: page(location=None)})
    observer = Observer()
    service = TrajectoryService(Store(), Registry(current), observer)

    initial = await service.snapshot(current.id)
    assert initial.panel_state.state is PanelState.WAITING
    assert initial.cursor is not None and initial.stream_id is not None
    assert initial.capabilities.features
    assert initial.overview.scope.value == "loaded"

    assert observer.capture is not None
    observer.capture(current.id, Batch(events=(event("live", 1),)))
    ready = await service.follow(
        current.id,
        stream_id=initial.stream_id,
        after=initial.cursor,
        wait=0,
        limit=20,
    )
    assert ready.panel_state is not None
    assert ready.panel_state.state is PanelState.READY
    assert [upsert.record.summary for upsert in ready.upserts] == ["live"]
    assert ready.capabilities is not None
    assert any(
        item.feature.value == "live_updates" and item.observed
        for item in ready.capabilities.features
    )
    assert ready.overview is not None and ready.overview.record_count == 1

    assert ready.cursor is not None
    observer.capture(current.id, Batch(error_code="source_failed", error="reader closed"))
    stale = await service.follow(
        current.id,
        stream_id=initial.stream_id,
        after=ready.cursor,
        wait=0,
        limit=20,
    )
    assert stale.upserts == ()
    assert stale.panel_state is not None
    assert stale.panel_state.state is PanelState.STALE
    assert "fresh snapshot" in stale.panel_state.message
    await service.aclose()


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            lambda registry, item: setattr(item, "status", Status.DEAD),
            TrajectoryParticipantState.DEAD,
        ),
        (
            lambda registry, item: setattr(item, "tier", Tier.EXTERNAL),
            TrajectoryParticipantState.EXTERNAL,
        ),
        (
            lambda registry, item: registry.participants.pop(item.id),
            TrajectoryParticipantState.MISSING,
        ),
    ],
)
async def test_follow_stops_for_terminal_or_non_addressable_participant(
    source_opener, change, expected
) -> None:
    current = participant()
    source_opener[current.id] = SourcePage({None: page(location=None)})
    registry = Registry(current)
    service = TrajectoryService(Store(), registry, Observer())
    initial = await service.snapshot(current.id)
    assert initial.cursor is not None and initial.stream_id is not None

    change(registry, current)
    delta = await asyncio.wait_for(
        service.follow(
            current.id,
            stream_id=initial.stream_id,
            after=initial.cursor,
            wait=30,
            limit=20,
        ),
        timeout=0.2,
    )

    assert delta.panel_state is not None
    assert delta.panel_state.participant_state is expected
    await service.aclose()


async def test_fresh_snapshot_recovers_an_untrusted_stream(source_opener) -> None:
    current = participant()
    source = SourcePage(
        {
            None: page(event("untrusted", 1), provenance="heuristic"),
        }
    )
    source_opener[current.id] = source
    service = TrajectoryService(Store(), Registry(current), Observer())

    first = await service.snapshot(current.id)
    assert first.panel_state.state is PanelState.UNTRUSTED

    source.pages[None] = page(event("trusted", 2))
    recovered = await service.snapshot(current.id)
    assert recovered.panel_state.state is PanelState.READY
    assert [record.summary for record in recovered.records] == ["trusted"]
    await service.aclose()


async def test_older_failure_keeps_its_cursor_and_warm_records_retryable(source_opener) -> None:
    current = participant()
    source = SourcePage(
        {
            None: page(event("latest", 1), older_cursor="older", has_older=True),
            "older": HistoryPage(error_code="source_failed", error="try again"),
        }
    )
    source_opener[current.id] = source
    service = TrajectoryService(Store(), Registry(current), Observer())

    first = await service.snapshot(current.id, limit=2)
    assert first.older_cursor is not None
    failed = await service.snapshot(current.id, before=first.older_cursor, limit=2)
    retried = await service.snapshot(current.id, before=first.older_cursor, limit=2)

    stream = service.streams[current.id]
    assert failed.panel_state.state is PanelState.STALE
    assert failed.has_older is True
    assert failed.older_cursor is not None
    assert stream.source_before == "older"
    assert [record.summary for record in stream.ring.records()] == ["latest"]
    assert retried.panel_state.state is PanelState.STALE
    assert source.calls == [None, "older", "older"]
    await service.aclose()
