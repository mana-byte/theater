"""Primary evidence is staged until delivery and replays after cancellation."""

from __future__ import annotations

import asyncio

import pytest

from tests.support.composite_sources import decl
from theater.harness.channels import CompositeSource, EnrichmentBinding
from theater.harness.contracts.runtime import (
    NativeTurnOutcome,
    NativeTurnTerminal,
    ResultCompleteness,
    ResultProvenance,
)
from theater.harness.contracts.source import Batch, Source


def _outcome(turn: str = "turn-1") -> NativeTurnOutcome:
    return NativeTurnOutcome(
        native_session_id="session-1",
        native_turn_id=turn,
        terminal=NativeTurnTerminal.COMPLETED,
        result="answer",
        completeness=ResultCompleteness.COMPLETE,
        provenance=ResultProvenance.NATIVE_EVIDENCE,
    )


class _EvidencePrimary(Source):
    def __init__(self) -> None:
        self.pending = True
        self.delivered = 0
        self.armed = 0

    async def read(self) -> Batch:
        return Batch()

    def pending_terminal_evidence(self) -> bool:
        return self.pending

    def terminal_evidence_delivered(self) -> None:
        self.delivered += 1
        self.pending = False

    def arm_terminal_evidence_replay(self) -> None:
        self.armed += 1


def test_exact_evidence_hooks_delegate_through_composite_primary() -> None:
    primary = _EvidencePrimary()
    composite = CompositeSource(primary=primary)

    assert composite.pending_terminal_evidence() is True
    composite.arm_terminal_evidence_replay()
    composite.terminal_evidence_delivered()

    assert primary.armed == 1
    assert primary.delivered == 1
    assert composite.pending_terminal_evidence() is False


@pytest.mark.asyncio
async def test_primary_evidence_is_staged_before_enrichment_await_and_replays_after_cancel():
    entered = asyncio.Event()

    class EvidencePrimary(Source):
        async def read(self) -> Batch:
            return Batch(terminal_evidence=(_outcome(),))

    class BlockingSource(Source):
        async def read(self) -> Batch:
            entered.set()
            await asyncio.Future()

    composite = CompositeSource(
        primary=EvidencePrimary(),
        enrichments=[EnrichmentBinding(source=BlockingSource(), declaration=decl())],
    )
    reading = asyncio.create_task(composite.read())
    await entered.wait()

    assert composite.pending_terminal_evidence() is True
    assert composite.terminal_evidence_snapshot() == (_outcome(),)
    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reading

    # No obsolete-primary reread or enrichment wait: the already-consumed
    # evidence remains the next bounded batch until persistence acknowledges it.
    replay = await composite.read()
    assert replay.terminal_evidence == (_outcome(),)
    composite.terminal_evidence_delivered()
    assert composite.pending_terminal_evidence() is False


@pytest.mark.asyncio
async def test_refresh_evidence_is_staged_until_delivery_acknowledgement():
    class RefreshEvidencePrimary(Source):
        async def read(self) -> Batch:
            return Batch()

        async def refresh(self) -> Batch:
            return Batch(terminal_evidence=(_outcome("refresh-turn"),))

    composite = CompositeSource(primary=RefreshEvidencePrimary())

    refreshed = await composite.refresh()

    assert refreshed.terminal_evidence == (_outcome("refresh-turn"),)
    assert composite.terminal_evidence_snapshot() == (_outcome("refresh-turn"),)
    assert (await composite.read()).terminal_evidence == (_outcome("refresh-turn"),)
    composite.terminal_evidence_delivered()
    assert composite.pending_terminal_evidence() is False


@pytest.mark.asyncio
async def test_inflight_refresh_forwards_primary_evidence_snapshot_after_cancel():
    entered = asyncio.Event()

    class RefreshHoldingPrimary(Source):
        def __init__(self) -> None:
            self.held: tuple[NativeTurnOutcome, ...] = ()

        async def read(self) -> Batch:
            return Batch()

        async def refresh(self) -> Batch:
            self.held = (_outcome("refresh-turn"),)
            entered.set()
            await asyncio.Future()

        def terminal_evidence_snapshot(self) -> tuple[NativeTurnOutcome, ...]:
            return self.held

    primary = RefreshHoldingPrimary()
    composite = CompositeSource(primary=primary)
    refreshing = asyncio.create_task(composite.refresh())
    await entered.wait()

    assert composite.terminal_evidence_snapshot() == (_outcome("refresh-turn"),)
    refreshing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await refreshing
    assert composite.terminal_evidence_snapshot() == (_outcome("refresh-turn"),)
