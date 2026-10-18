"""Shared rigs for the live-observation registration tests."""

from __future__ import annotations

import asyncio

from tests.support.control_harness import make_runtime
from tests.support.observation import (
    LIVE_CHANNEL,
    FakeHarness,
    RecordingJobs,
    minimal_gates,
)
from theater.daemon.controls import ControlService
from theater.daemon.observation.live import LiveRegistration
from theater.daemon.persistence.repositories.native_evidence import (
    NativeTerminalEvidence,
)
from theater.harness.contracts.runtime import (
    LiveChannelDeclaration,
    NativeTurnOutcome,
    NativeTurnTerminal,
    ResultCompleteness,
    ResultProvenance,
)
from theater.harness.source import Batch, Source
from theater.models import now


class BlockingEnrichment(Source):
    """An enrichment that exposes the primary-read/enrichment-read boundary."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.closed = False

    async def read(self) -> Batch:
        self.entered.set()
        await asyncio.Future()

    async def aclose(self) -> None:
        self.closed = True


def outcome(turn: str, session: str) -> NativeTurnOutcome:
    return NativeTurnOutcome(
        native_session_id=session,
        native_turn_id=turn,
        terminal=NativeTurnTerminal.COMPLETED,
        result="the answer",
        completeness=ResultCompleteness.COMPLETE,
        provenance=ResultProvenance.NATIVE_EVIDENCE,
    )


def evidence_row(pid: str, generation: int, session: str, turn: str) -> NativeTerminalEvidence:
    return NativeTerminalEvidence(
        participant_id=pid,
        backend_generation=generation,
        native_session_id=session,
        native_turn_id=turn,
        terminal=NativeTurnTerminal.COMPLETED,
        result="the answer",
        completeness=ResultCompleteness.COMPLETE,
        provenance=ResultProvenance.NATIVE_EVIDENCE,
        recorded_at=now(),
    )


def bus_texts(store) -> list[str]:
    return [
        text
        for row in store.bus_tail(limit=500)
        if isinstance((payload := row["payload"]), dict)
        and isinstance((text := payload.get("text")), str)
    ]


def touch_paths(store, handle: str) -> list[str]:
    from sqlalchemy import select

    from theater.daemon.schema import touch as touch_table

    return [
        row.path
        for row in store.conn.execute(
            select(touch_table).where(touch_table.c.job_handle == handle)
        ).fetchall()
    ]


class ArrivingSource(Source):
    """A live source whose data arrival fires the installed activity callback.

    ``deliver`` is the receive loop: bounded data lands and the arrival is
    announced through the callback the hub installed — no wake call from
    the test, no task per message.
    """

    def __init__(self) -> None:
        self._queue: list[Batch] = []
        self._activity = None
        self.arrivals = 0

    def set_activity_callback(self, callback) -> None:
        self._activity = callback

    def deliver(self, batch: Batch) -> None:
        self._queue.append(batch)
        self.arrivals += 1
        if self._activity is not None:
            self._activity()

    async def read(self) -> Batch:
        return self._queue.pop(0) if self._queue else Batch()


class CountingBatchSource(Source):
    """A draining live source that records whether backpressure stopped reads."""

    def __init__(self, *batches: Batch) -> None:
        self.batches = list(batches)
        self.reads = 0

    async def read(self) -> Batch:
        self.reads += 1
        return self.batches.pop(0) if self.batches else Batch()


class LiveOnlyRig:
    """A live-only observer (no durable reader) over one store and control service."""

    def __init__(self, store, registry, *, poll: float = 0.02):
        from theater.daemon.observer import Observer

        self.store = store
        self.jobs = RecordingJobs(store)
        self.runtime = make_runtime("p1")
        self.observer = Observer(
            registry,
            {"fake": FakeHarness(has_transcript=False)},
            poll=poll,
            search=poll,
            sync=poll,
            jobs=self.jobs,
        )
        self.service = ControlService(
            store=store,
            jobs=self.jobs,
            runtime_for=lambda pid: self.runtime if pid == "p1" else None,
            gates=minimal_gates(),
        )
        self.observer.start()

    def register(self, evidence_sink) -> None:
        self.observer.live.register(
            LiveRegistration(
                participant_id="p1",
                live_source=self.runtime.live_source(),
                channel=LiveChannelDeclaration(channel=LIVE_CHANNEL),
                backend_generation=self.runtime.state.backend_generation,
                native_session_id=self.runtime.state.native_session_id,
                evidence_sink=evidence_sink,
                active_job_for_turn=self.service.active_job_for_native_turn,
            )
        )

    def retained_evidence_count(self) -> int:
        return len(self.observer._pending_evidence.get("p1", ()))
