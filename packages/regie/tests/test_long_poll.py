"""LongPollLoop: primes without blocking, then long-polls; failures back off."""

import asyncio

import pytest
from regie.controllers import long_poll
from regie.controllers.long_poll import LongPollLoop


async def test_primes_then_long_polls_and_stops_cleanly() -> None:
    waits: list[float] = []
    second = asyncio.Event()

    async def step(wait: float) -> bool:
        waits.append(wait)
        if len(waits) > 1:
            second.set()
            await asyncio.Event().wait()  # the daemon-side block
        return True

    loop = LongPollLoop(step, wait_seconds=25.0, name="t")
    loop.start()
    await asyncio.wait_for(second.wait(), 2)
    assert waits == [0.0, 25.0]
    await loop.close()
    assert not loop.running


async def test_failures_back_off_instead_of_hot_looping(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []
    done = asyncio.Event()

    async def sleep(delay: float) -> None:
        if delay:
            delays.append(delay)
        if len(delays) == 5:
            done.set()
            await asyncio.Event().wait()

    async def step(_wait: float) -> bool:
        return False

    monkeypatch.setattr(long_poll.asyncio, "sleep", sleep)
    loop = LongPollLoop(step, wait_seconds=25.0, name="t")
    loop.start()
    await asyncio.wait_for(done.wait(), 2)
    assert delays == [1.0, 2.0, 4.0, 8.0, 10.0]
    await loop.close()
