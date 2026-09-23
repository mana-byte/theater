from __future__ import annotations

import asyncio

import pytest
from regie.bridge import timing

from theater.frontend import CallbackRequest, CallbackResponse


def _request(method: str, **params: object) -> CallbackRequest:
    return CallbackRequest(
        callback_id="1",
        method=method,
        provider_generation=1,
        params={"operation_id": "op", "terminal_id": "%1", **params},
    )


def _clock(monkeypatch, *values: float) -> None:
    ticks = iter(values)
    monkeypatch.setattr(timing, "monotonic", lambda: next(ticks))


def test_mutation_logs_phases_and_result(monkeypatch, caplog):
    caplog.set_level("INFO", logger="regie.bridge.latency")
    # trace start, phase start, phase end, emit
    _clock(monkeypatch, 1.0, 1.010, 1.030, 1.050)

    async def handler(_request):
        with timing.phase("effect"):
            pass
        return {"delivery": "accepted"}

    traced = timing.timed("terminal.terminate", handler)
    result = asyncio.run(traced(_request("terminal.terminate")))
    assert result == {"delivery": "accepted"}
    assert caplog.messages == [
        "callback.terminal.terminate 50.0ms operation=op terminal=%1 result=success effect_ms=20.0"
    ]


def test_fast_successful_reads_are_not_logged(monkeypatch, caplog):
    caplog.set_level("INFO", logger="regie.bridge.latency")
    _clock(monkeypatch, 1.0, 1.020)

    async def handler(_request):
        return {"terminal": {}}

    asyncio.run(timing.timed("terminal.inspect", handler)(_request("terminal.inspect")))
    assert caplog.messages == []


def test_slow_reads_and_refusals_are_logged(monkeypatch, caplog):
    caplog.set_level("INFO", logger="regie.bridge.latency")
    _clock(monkeypatch, 1.0, 1.0 + timing.SLOW_READ_MS / 1000 + 0.001)

    async def refuse(_request):
        return CallbackResponse(error={"code": "human_present", "message": "no"})

    asyncio.run(timing.timed("terminal.inspect", refuse)(_request("terminal.inspect")))
    assert caplog.messages[0].startswith("callback.terminal.inspect ")
    assert caplog.messages[0].endswith("result=human_present")


def test_exceptions_propagate_and_are_recorded(monkeypatch, caplog):
    caplog.set_level("INFO", logger="regie.bridge.latency")
    error = RuntimeError("tmux failed")

    async def fail(_request):
        raise error

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(timing.timed("terminal.deliver", fail)(_request("terminal.deliver")))
    assert caught.value is error
    assert caplog.messages[0].endswith("result=exception")


def test_phase_outside_a_callback_is_a_noop():
    with timing.phase("orphan"):
        pass
