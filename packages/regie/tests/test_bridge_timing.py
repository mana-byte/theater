from __future__ import annotations

import asyncio

import pytest
from regie.bridge import timing

from theater.frontend import CallbackRequest, CallbackResponse


def _call(monkeypatch, method: str, handler, *ticks: float):
    clock = iter(ticks)
    monkeypatch.setattr(timing, "monotonic", lambda: next(clock))
    request = CallbackRequest("1", method, {"operation_id": "op", "terminal_id": "%1"}, 1)
    return asyncio.run(timing.timed_handlers({method: handler})[method](request))


def test_mutation_logs_phases_and_result(monkeypatch, caplog):
    caplog.set_level("INFO", logger="regie.bridge.latency")

    async def handler(_request):
        with timing.phase("effect"):
            pass
        return {"delivery": "accepted"}

    result = _call(monkeypatch, "terminal.terminate", handler, 1.0, 1.010, 1.030, 1.050)
    assert result == {"delivery": "accepted"}
    assert caplog.messages == [
        "callback.terminal.terminate 50.0ms operation_id=op terminal_id=%1 "
        "result=success effect_ms=20.0"
    ]


def test_fast_successful_reads_are_not_logged(monkeypatch, caplog):
    caplog.set_level("INFO", logger="regie.bridge.latency")

    async def handler(_request):
        return {"terminal": {}}

    _call(monkeypatch, "terminal.inspect", handler, 1.0, 1.020)
    assert caplog.messages == []


def test_slow_or_refused_reads_are_logged(monkeypatch, caplog):
    caplog.set_level("INFO", logger="regie.bridge.latency")

    async def refuse(_request):
        return CallbackResponse(error={"code": "human_present", "message": "no"})

    _call(monkeypatch, "terminal.inspect", refuse, 1.0, 1.001)
    assert caplog.messages[0].endswith("result=human_present")


def test_exceptions_propagate_and_are_recorded(monkeypatch, caplog):
    caplog.set_level("INFO", logger="regie.bridge.latency")
    error = RuntimeError("tmux failed")

    async def fail(_request):
        raise error

    with pytest.raises(RuntimeError) as caught:
        _call(monkeypatch, "terminal.deliver", fail, 1.0, 1.001)
    assert caught.value is error
    assert caplog.messages[0].endswith("result=exception")


def test_phase_outside_a_callback_is_a_noop():
    with timing.phase("orphan"):
        pass
