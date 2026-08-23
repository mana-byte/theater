from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from theater.constants.daemon import BUS_KIND_PARTICIPANT_KILL_REQUESTED
from theater.daemon.trajectory import history as history_module
from theater.daemon.trajectory.runtime import TrajectoryRuntime
from theater.daemon.trajectory.service import TrajectoryService
from theater.harness.contracts.events import Event, EventKind
from theater.harness.contracts.source import Batch, HistoryPage, Source
from theater.models import NotFound, Participant, Status, Tier


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

    def emit(self, row: dict) -> None:
        self.rows.append(dict(row))
        for listener in tuple(self.listeners):
            listener(dict(row))

    def bus_page_for_participant(self, participant_id, *, before_id=None, limit, kinds):
        return [
            row
            for row in self.rows
            if participant_id in {row.get("from_id"), row.get("to_id")}
            and row.get("kind") in kinds
            and (before_id is None or row["id"] < before_id)
        ][-limit:]


class Registry:
    def __init__(self, participant: Participant) -> None:
        self.participant = participant

    def resolve(self, token: str) -> Participant:
        return self.get(token)

    def get(self, participant_id: str) -> Participant:
        if participant_id != self.participant.id:
            raise NotFound(f"no participant {participant_id!r}")
        return self.participant


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


async def test_bus_listener_is_lazy_removable_and_reregisterable() -> None:
    store = Store()
    current = participant()
    runtime = TrajectoryRuntime(store, Registry(current))
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
    current = participant()
    store = Store()
    source = BlockingSource()
    observer = Observer()
    monkeypatch.setattr(
        history_module,
        "open_participant_source",
        lambda _observer, **_kwargs: source,
    )
    service = TrajectoryService(store, Registry(current), observer)

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
