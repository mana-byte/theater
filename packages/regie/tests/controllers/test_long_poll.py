"""LongPollLoop: primes without blocking, then long-polls; failures back off."""

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest
from regie import bus as bus_module
from regie.bus import DiagnosticBusController
from regie.controllers import long_poll
from regie.controllers.long_poll import LongPollLoop

from theater.frontend import ErrorCode, ErrorValue, FrontendClient, FrontendResponseError


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


class _Tail:
    def __init__(self, *replies: object) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, object]] = []

    async def bus_tail(self, **params: object) -> object:
        self.calls.append(params)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _page(items: tuple[dict, ...], next_after_id: int | None = None) -> object:
    extra = {} if next_after_id is None else {"next_after_id": next_after_id}
    return SimpleNamespace(value=SimpleNamespace(items=items, extra=extra))


def _controller(tail: _Tail) -> DiagnosticBusController:
    client = SimpleNamespace(diagnostics=tail)
    return DiagnosticBusController(cast(FrontendClient, client), batch=50)


async def test_filtered_reader_primes_then_sends_kinds_and_follows_scan_cursor() -> None:
    tail = _Tail(_page(({"id": 9, "kind": "x"},), 9), _page((), 400), _page(({"id": 401},), 401))
    controller = _controller(tail)
    kinds = ("agent.send",)
    assert await controller.poll(0.0, kinds=kinds) == ()
    assert await controller.poll(25.0, kinds=kinds) == ()
    assert controller.after_id == 400 and controller.last_gap == 0
    assert len(await controller.poll(25.0, kinds=kinds)) == 1
    assert tail.calls[0] == {"after_id": 0, "limit": 1}
    assert tail.calls[1] == {
        "after_id": 9,
        "limit": 50,
        "kinds": ["agent.send"],
        "wait_seconds": 25.0,
    }


async def test_previous_daemon_falls_back_to_bounded_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    refusal = FrontendResponseError.__new__(FrontendResponseError)
    Exception.__init__(refusal)
    refusal.value = ErrorValue(ErrorCode.BAD_REQUEST, "unknown parameter")
    rows = ({"id": 3, "kind": "agent.send"}, {"id": 4, "kind": "other"})
    tail = _Tail(_page(()), refusal, _page(rows), _page(()))
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(bus_module.asyncio, "sleep", sleep)
    controller = _controller(tail)
    kinds = ("agent.send",)
    await controller.poll(0.0, kinds=kinds)
    assert [r["id"] for r in await controller.poll(25.0, kinds=kinds)] == [3]
    assert controller.after_id == 4
    assert await controller.poll(25.0, kinds=kinds) == ()
    assert "wait_seconds" not in tail.calls[-1] and sleeps == [1.0]
