"""Panel state: initial derivation from history and liveness, and live follow transitions."""

from __future__ import annotations

import asyncio
from types import MappingProxyType, SimpleNamespace

import pytest

from tests.support.trajectory_service import (
    FakeBusStore,
    FakeObserver,
    FakeRegistry,
    assistant_event,
    trajectory_participant,
)
from theater.daemon.trajectory.history import HistoryLoad
from theater.daemon.trajectory.panel import initial_panel_state, participant_state
from theater.daemon.trajectory.service import TrajectoryService
from theater.harness.contracts.events import Event, EventKind
from theater.harness.contracts.source import Batch, HistoryPage, Source
from theater.models import Participant, Status, Tier
from theater.trajectory import (
    PanelState,
    PanelStateInfo,
    TrajectoryCapabilities,
    TrajectoryDelta,
    TrajectoryFeature,
    TrajectoryParticipantState,
    TrajectorySupport,
    TrajectoryValidationError,
)
from theater.transcript_identity import TRANSCRIPT_IDENTITY_LOST_CODE


def _participant(*, status: Status = Status.IDLE, tier: Tier = Tier.ADOPTED) -> Participant:
    return Participant(id="p", harness="fake", status=status, tier=tier)


@pytest.mark.parametrize(
    ("status", "tier", "expected"),
    [
        (Status.IDLE, Tier.ADOPTED, TrajectoryParticipantState.LIVE),
        (Status.DEAD, Tier.ADOPTED, TrajectoryParticipantState.DEAD),
        (Status.IDLE, Tier.EXTERNAL, TrajectoryParticipantState.EXTERNAL),
    ],
)
def test_participant_state_is_pure(
    status: Status, tier: Tier, expected: TrajectoryParticipantState
) -> None:
    assert participant_state(_participant(status=status, tier=tier)) is expected


def test_initial_panel_is_ready_for_trusted_history() -> None:
    result = HistoryLoad(
        HistoryPage(location="/tmp/p", events=(Event(kind=EventKind.ASSISTANT, text="ok"),)),
        source_epoch="epoch",
        trusted=True,
        message="loaded",
    )

    panel = initial_panel_state(
        _participant(),
        current_participant_state=TrajectoryParticipantState.LIVE,
        result=result,
        has_transcript=False,
        live_allowed=False,
    )

    assert panel == PanelStateInfo(PanelState.READY, "loaded", TrajectoryParticipantState.LIVE)


def test_initial_panel_waits_for_an_empty_trusted_history() -> None:
    result = HistoryLoad(HistoryPage(), source_epoch="epoch", trusted=True)

    panel = initial_panel_state(
        _participant(),
        current_participant_state=TrajectoryParticipantState.LIVE,
        result=result,
        has_transcript=False,
        live_allowed=False,
    )

    assert panel == PanelStateInfo(
        PanelState.WAITING,
        "the transcript source is waiting for its first record",
        TrajectoryParticipantState.LIVE,
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [(Status.IDLE, PanelState.UNTRUSTED), (Status.DEAD, PanelState.UNAVAILABLE)],
)
def test_initial_panel_handles_lost_identity_by_participant_liveness(
    status: Status, expected: PanelState
) -> None:
    result = HistoryLoad(
        HistoryPage(error_code=TRANSCRIPT_IDENTITY_LOST_CODE),
        source_epoch=None,
        trusted=False,
        message="rebind transcript",
    )

    panel = initial_panel_state(
        _participant(status=status),
        current_participant_state=TrajectoryParticipantState.LIVE,
        result=result,
        has_transcript=False,
        live_allowed=False,
    )

    assert panel == PanelStateInfo(expected, "rebind transcript", TrajectoryParticipantState.LIVE)


def test_initial_panel_rejects_untrusted_provenance() -> None:
    result = HistoryLoad(
        HistoryPage(provenance="heuristic"),
        source_epoch=None,
        trusted=False,
    )

    panel = initial_panel_state(
        _participant(),
        current_participant_state=TrajectoryParticipantState.LIVE,
        result=result,
        has_transcript=False,
        live_allowed=False,
    )

    assert panel == PanelStateInfo(
        PanelState.UNTRUSTED,
        "transcript identity is not trusted",
        TrajectoryParticipantState.LIVE,
    )


def test_initial_panel_rejects_ambiguous_history() -> None:
    result = HistoryLoad(
        HistoryPage(provenance="operator"),
        source_epoch=None,
        trusted=False,
        ambiguous=True,
        message="bind the session",
    )

    panel = initial_panel_state(
        _participant(),
        current_participant_state=TrajectoryParticipantState.LIVE,
        result=result,
        has_transcript=False,
        live_allowed=False,
    )

    assert panel == PanelStateInfo(
        PanelState.UNTRUSTED,
        "bind the session",
        TrajectoryParticipantState.LIVE,
    )


def test_initial_panel_retains_cached_transcript_records_after_failure() -> None:
    result = HistoryLoad(
        HistoryPage(error_code="source_failed", error="reader closed"),
        source_epoch=None,
        trusted=False,
        message="reader closed",
    )

    panel = initial_panel_state(
        _participant(),
        current_participant_state=TrajectoryParticipantState.LIVE,
        result=result,
        has_transcript=True,
        live_allowed=False,
    )

    assert panel == PanelStateInfo(
        PanelState.STALE,
        "transcript history is unavailable; cached records remain (reader closed)",
        TrajectoryParticipantState.LIVE,
    )


def test_initial_panel_is_unavailable_without_history_or_cache() -> None:
    result = HistoryLoad(
        HistoryPage(error_code="source_failed"),
        source_epoch=None,
        trusted=False,
    )

    panel = initial_panel_state(
        _participant(),
        current_participant_state=TrajectoryParticipantState.LIVE,
        result=result,
        has_transcript=False,
        live_allowed=False,
    )

    assert panel == PanelStateInfo(
        PanelState.UNAVAILABLE,
        "trajectory history is unavailable",
        TrajectoryParticipantState.LIVE,
    )


class SourcePage(Source):
    def __init__(self, pages: dict[str | None, HistoryPage]) -> None:
        self.pages = pages
        self.calls: list[str | None] = []

    async def read(self) -> Batch:
        return Batch()

    async def history_page(self, *, before: str | None = None, limit: int = 200) -> HistoryPage:
        self.calls.append(before)
        return self.pages[before]


def page(*events: Event, provenance: str = "operator", **kwargs) -> HistoryPage:
    return HistoryPage(
        location=kwargs.pop("location", "/tmp/p"),
        events=events,
        cursor="cursor",
        provenance=provenance,
        **kwargs,
    )


def test_delta_panel_state_wire_is_optional_and_validated() -> None:
    old = TrajectoryDelta.from_wire({"stream_id": "stream"})
    assert old == TrajectoryDelta(stream_id="stream")

    panel = PanelStateInfo(PanelState.STALE, "retry the snapshot")
    delta = TrajectoryDelta(stream_id="stream", panel_state=panel)
    assert TrajectoryDelta.from_wire(delta.to_wire()) == delta

    with pytest.raises(TrajectoryValidationError):
        TrajectoryDelta.from_wire({"stream_id": "stream", "panel_state": {"state": "bad"}})


async def test_follow_reports_waiting_to_ready_and_state_only_stale_delta(source_opener) -> None:
    current = trajectory_participant()
    source_opener[current.id] = SourcePage({None: page(location=None)})
    observer = FakeObserver()
    service = TrajectoryService(FakeBusStore(), FakeRegistry([current]), observer)

    initial = await service.snapshot(current.id)
    assert initial.panel_state.state is PanelState.WAITING
    assert initial.cursor is not None and initial.stream_id is not None
    assert initial.capabilities == TrajectoryCapabilities()
    assert initial.overview.scope_complete

    assert observer.capture is not None
    observer.capture(current.id, Batch(events=(assistant_event("live", 1),)))
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
    assert TrajectoryFeature.LIVE_UPDATES in ready.capabilities.observed
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


async def test_empty_live_batch_does_not_observe_live_trajectory_updates(source_opener) -> None:
    current = trajectory_participant()
    source_opener[current.id] = SourcePage({None: page(location=None)})
    observer = FakeObserver()
    service = TrajectoryService(FakeBusStore(), FakeRegistry([current]), observer)

    initial = await service.snapshot(current.id)
    assert initial.cursor is not None and initial.stream_id is not None
    assert observer.capture is not None
    observer.capture(current.id, Batch(progressed=True))
    delta = await service.follow(
        current.id,
        stream_id=initial.stream_id,
        after=initial.cursor,
        wait=0,
        limit=20,
    )

    assert delta.capabilities is not None
    assert TrajectoryFeature.LIVE_UPDATES not in delta.capabilities.observed
    await service.aclose()


async def test_capability_declaration_accepts_mapping_harness_registries(source_opener) -> None:
    current = trajectory_participant()
    source_opener[current.id] = SourcePage({None: page(location=None)})
    observer = FakeObserver()
    observer.harnesses = MappingProxyType(
        {
            "fake": SimpleNamespace(
                observer=SimpleNamespace(
                    trajectory_capabilities=TrajectoryCapabilities(
                        supported=frozenset({TrajectoryFeature.MODELS})
                    )
                )
            )
        }
    )
    service = TrajectoryService(FakeBusStore(), FakeRegistry([current]), observer)

    snapshot = await service.snapshot(current.id)

    assert (
        snapshot.capabilities.support_for(TrajectoryFeature.MODELS) is TrajectorySupport.SUPPORTED
    )
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
    current = trajectory_participant()
    source_opener[current.id] = SourcePage({None: page(location=None)})
    registry = FakeRegistry([current])
    service = TrajectoryService(FakeBusStore(), registry, FakeObserver())
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
        timeout=5.0,
    )

    assert delta.panel_state is not None
    assert delta.panel_state.participant_state is expected
    await service.aclose()


async def test_fresh_snapshot_recovers_an_untrusted_stream(source_opener) -> None:
    current = trajectory_participant()
    source = SourcePage(
        {
            None: page(assistant_event("untrusted", 1), provenance="heuristic"),
        }
    )
    source_opener[current.id] = source
    service = TrajectoryService(FakeBusStore(), FakeRegistry([current]), FakeObserver())

    first = await service.snapshot(current.id)
    assert first.panel_state.state is PanelState.UNTRUSTED

    source.pages[None] = page(assistant_event("trusted", 2))
    recovered = await service.snapshot(current.id)
    assert recovered.panel_state.state is PanelState.READY
    assert [record.summary for record in recovered.records] == ["trusted"]
    await service.aclose()


async def test_older_failure_keeps_its_cursor_and_warm_records_retryable(source_opener) -> None:
    current = trajectory_participant()
    source = SourcePage(
        {
            None: page(assistant_event("latest", 1), older_cursor="older", has_older=True),
            "older": HistoryPage(error_code="source_failed", error="try again"),
        }
    )
    source_opener[current.id] = source
    service = TrajectoryService(FakeBusStore(), FakeRegistry([current]), FakeObserver())

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
