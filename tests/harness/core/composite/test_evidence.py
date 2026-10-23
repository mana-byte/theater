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


class _EmptySource(Source):
    """A source implementing only the abstract seam, so every default applies."""

    async def read(self) -> Batch:
        return Batch()


class _SnapshotOnlySource(_EmptySource):
    """A snapshot-only source: it retains evidence without the pending flag."""

    def __init__(self) -> None:
        self.held: tuple[NativeTurnOutcome, ...] = ()

    def terminal_evidence_snapshot(self) -> tuple[NativeTurnOutcome, ...]:
        return self.held


def test_default_source_lifecycle_defaults_forward_through_composite() -> None:
    composite = CompositeSource(primary=_EmptySource())

    assert composite.pending_terminal_evidence() is False
    assert composite.terminal_evidence_snapshot() == ()

    # Default delivered is a no-op, and the optional replay hook stays optional.
    composite.terminal_evidence_delivered()
    composite.arm_terminal_evidence_replay()

    assert composite.pending_terminal_evidence() is False


def test_snapshot_only_primary_holds_the_composite_checkpoint_by_snapshot() -> None:
    primary = _SnapshotOnlySource()
    primary.held = (_outcome("snapshot-turn"),)
    composite = CompositeSource(primary=primary)

    # The direct snapshot-only source keeps the contract's default answer.
    assert primary.pending_terminal_evidence() is False
    # The wrapper still holds the checkpoint: retained evidence counts as
    # pending even though the primary's own flag is False.
    assert composite.pending_terminal_evidence() is True
    assert composite.terminal_evidence_snapshot() == (_outcome("snapshot-turn"),)


def test_explicit_false_pending_with_snapshot_still_holds_the_composite_checkpoint() -> None:
    class ExplicitFalseSnapshot(_SnapshotOnlySource):
        def pending_terminal_evidence(self) -> bool:
            return False

    primary = ExplicitFalseSnapshot()
    primary.held = (_outcome("explicit-false-turn"),)
    composite = CompositeSource(primary=primary)

    assert composite.pending_terminal_evidence() is True

    primary.held = ()
    assert composite.pending_terminal_evidence() is False


@pytest.mark.asyncio
async def test_nested_wrappers_forward_the_evidence_lifecycle() -> None:
    class EvidencePrimary(_EmptySource):
        async def read(self) -> Batch:
            return Batch(terminal_evidence=(_outcome("nested-turn"),))

    inner = CompositeSource(primary=EvidencePrimary())
    await inner.read()
    outer = CompositeSource(primary=inner)

    assert outer.pending_terminal_evidence() is True
    assert outer.terminal_evidence_snapshot() == (_outcome("nested-turn"),)

    outer.terminal_evidence_delivered()

    assert outer.pending_terminal_evidence() is False
    assert inner.pending_terminal_evidence() is False
