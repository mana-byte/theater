"""Helpers shared across a split test module."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from tests.support.pi_bridge import (
    GENERATION,
    PARTICIPANT,
)
from theater.harness.builtin.plugins.pi.runtime import PiFrontendPeer, PiFrontendRuntime


def make_runtime(peer: PiFrontendPeer) -> PiFrontendRuntime:
    return PiFrontendRuntime(
        participant_id=PARTICIPANT,
        backend_generation=GENERATION,
        peer=peer,
    )


async def eventually(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")
