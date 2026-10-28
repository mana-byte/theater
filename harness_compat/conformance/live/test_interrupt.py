"""Interrupting a held turn returns the CLI to a ready prompt, and the next turn still works."""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from harness_compat.contract import MockReply

pytestmark = [pytest.mark.integration, pytest.mark.family("interrupt")]


async def test_interrupt_ends_a_held_turn_and_the_next_turn_still_works(live, obligation):
    participant = await live.spawn(live.approval)
    await live.ready(participant)
    held = await live.hold_turn(participant, "Take your time")

    await live.interrupt(participant)

    await live.expect_prompt(participant)
    assert await asyncio.to_thread(_wait, lambda: held.hold.cancelled, 30), (
        "the CLI kept its request"
    )
    assert (await live.job(held.handle)).state in ("done", "killed", "crashed")
    nonce = f"AFTER-{uuid.uuid4().hex[:8]}"
    live.queue(MockReply(nonce))
    job = await live.send(participant, f"Reply with {nonce}")
    assert job.state == "done", job.error
    assert nonce in str(job.result)


def _wait(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()
