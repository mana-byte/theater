"""Fake bus store, registry, observer and history sources for the trajectory service tests."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from theater.harness.contracts.events import Event, EventKind
from theater.harness.contracts.source import Batch, HistoryPage, Source
from theater.models import NotFound, Participant, Status, Tier
from theater.trajectory import (
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
)


class FakeBusStore:
    def __init__(self) -> None:
        self.listeners: list = []
        self.rows: list[dict] = []
        self.record_lookups: list[tuple[str, int, frozenset[str]]] = []

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

    def bus_record_for_participant(self, participant_id, row_id, *, kinds):
        self.record_lookups.append((participant_id, row_id, frozenset(kinds)))
        return next(
            (
                dict(row)
                for row in self.rows
                if row.get("id") == row_id
                and participant_id in {row.get("from_id"), row.get("to_id")}
                and row.get("kind") in kinds
            ),
            None,
        )


class FakeRegistry:
    def __init__(self, participants: list[Participant]) -> None:
        self.participants = {participant.id: participant for participant in participants}

    def resolve(self, token: str) -> Participant:
        participant = self.participants.get(token)
        if participant is None:
            raise NotFound(f"no participant {token!r}")
        return participant

    def get(self, participant_id: str) -> Participant:
        return self.resolve(participant_id)


class FakeObserver:
    def __init__(self) -> None:
        self.capture = None
        self.harnesses = {"fake": SimpleNamespace(observer=object())}

    def set_trajectory_capture(self, callback) -> None:
        self.capture = callback

    def transcript_identity_lost(self, _participant_id: str) -> bool:
        return False

    def history_is_ambiguous(self, _participant_id: str, _history) -> bool:
        return False


class FakeSource(Source):
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


class FakePagedSource(FakeSource):
    def __init__(self, pages: dict[str | None, HistoryPage]) -> None:
        super().__init__(pages[None])
        self.pages = pages

    async def history_page(self, *, before: str | None = None, limit: int = 200) -> HistoryPage:
        self.calls.append(before)
        self.thread_ids.append(threading.get_ident())
        return self.pages[before]


def trajectory_participant(
    participant_id: str = "p",
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


def assistant_event(text: str, index: int, *, offset: int | None = None) -> Event:
    return Event(
        kind=EventKind.ASSISTANT,
        text=text,
        raw_index=index,
        source_offset=offset,
    )


def history_page(*events: Event, provenance: str = "operator", location: str | None = "/tmp/p"):
    return HistoryPage(
        location=location,
        events=events,
        cursor="cursor",
        provenance=provenance,
    )


def wire_size(value: dict) -> int:
    return len(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def snapshot_record(record_id: str, *, revision: int = 0, summary: str = "x") -> TrajectoryRecord:
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
