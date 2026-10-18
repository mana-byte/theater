"""A scripted live or durable source and the native outcome it reports."""

from __future__ import annotations

import asyncio

from theater.harness.contracts.channels import (
    ChannelDeclaration,
    ChannelKind,
)
from theater.harness.contracts.runtime import (
    NativeTurnOutcome,
    NativeTurnTerminal,
    ResultCompleteness,
    ResultProvenance,
)
from theater.harness.contracts.source import Batch, Source

LIVE = ChannelDeclaration(id="codex-live", kind=ChannelKind.LIVE)


def outcome(turn: str = "turn-1", *, session: str = "session-1") -> NativeTurnOutcome:
    return NativeTurnOutcome(
        native_session_id=session,
        native_turn_id=turn,
        terminal=NativeTurnTerminal.COMPLETED,
        result="the answer",
        completeness=ResultCompleteness.COMPLETE,
        provenance=ResultProvenance.NATIVE_EVIDENCE,
    )


class ScriptedSource(Source):
    """A Source that drains scripted batches and records its lifecycle."""

    def __init__(
        self,
        *batches: Batch,
        live_delay: float = 0.0,
        fail_reads: bool = False,
    ) -> None:
        self.batches = list(batches)
        self.live_delay = live_delay
        self.fail_reads = fail_reads
        self.closed = False
        self.acked = False
        self.rolled_back = False
        self.checkpoint: str | None = None
        self.pending_checkpoint: str | None = None

    async def read(self) -> Batch:
        if self.fail_reads:
            raise RuntimeError("channel exploded")
        if self.live_delay:
            await asyncio.sleep(self.live_delay)
        return self.batches.pop(0) if self.batches else Batch()

    async def refresh(self) -> Batch:
        return self.batches.pop(0) if self.batches else Batch()

    def source_checkpoint(self) -> str | None:
        return self.checkpoint

    def pending_source_checkpoint(self) -> str | None:
        return self.pending_checkpoint

    def acknowledge_source_checkpoint(self) -> None:
        self.acked = True
        self.pending_checkpoint = None

    def rollback_source_checkpoint(self) -> None:
        self.rolled_back = True

    async def aclose(self) -> None:
        self.closed = True
