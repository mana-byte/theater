"""Stub sources and sinks for the composite-source tests."""

from __future__ import annotations

import asyncio

from theater.harness.contracts.channels import ChannelCapability, ChannelDeclaration, ChannelKind
from theater.harness.contracts.source import Batch, Source
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.trajectory.enums import TrajectoryKind


def make_fact(
    native_id: str | None = None,
    revision: int = 0,
    kind: TrajectoryKind = TrajectoryKind.TOOL_CALL,
) -> TrajectoryFact:
    return TrajectoryFact(kind=kind, native_id=native_id, revision=revision)


def decl(
    channel_id: str = "hook",
    kind: ChannelKind = ChannelKind.HOOK,
    capabilities: tuple[ChannelCapability, ...] = (),
) -> ChannelDeclaration:
    return ChannelDeclaration(id=channel_id, kind=kind, capabilities=capabilities)


class ControlledSource(Source):
    def __init__(self) -> None:
        self._batch: Batch = Batch()
        self.read_count = 0

    async def read(self) -> Batch:
        self.read_count += 1
        return self._batch

    def set_batch(self, batch: Batch) -> None:
        self._batch = batch


class RecordingSource(Source):
    def __init__(self) -> None:
        self.closed = False
        self.close_count = 0

    async def read(self) -> Batch:
        return Batch()

    async def aclose(self) -> None:
        self.close_count += 1
        self.closed = True


class BoomSource(Source):
    async def read(self) -> Batch:
        raise ValueError("secret-enrichment-value")


class SlowSource(Source):
    def __init__(self, delay: float = 10.0) -> None:
        self._delay = delay

    async def read(self) -> Batch:
        await asyncio.sleep(self._delay)
        return Batch()
