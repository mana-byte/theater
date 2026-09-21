from __future__ import annotations

import asyncio
from contextlib import nullcontext

import pytest
from regie import latency
from regie.controllers.actions import ActionRecord


@pytest.mark.parametrize(
    ("error", "result"),
    [(None, "success"), (RuntimeError("failed"), "error"), (asyncio.CancelledError(), "cancelled")],
)
@pytest.mark.parametrize("startup", [False, True])
def test_phase_preserves_outcomes(monkeypatch, caplog, error, result, startup):
    caplog.set_level("INFO", logger="regie.latency")
    ticks = iter((10.0, 10.025))
    monkeypatch.setattr(latency, "monotonic", lambda: next(ticks))
    record = ActionRecord("spawn", "target", "key", operation_id="operation")

    expected = pytest.raises(type(error)) if error is not None else nullcontext()
    phase = (
        latency.startup_phase("snapshot") if startup else latency.action_phase(record, "snapshot")
    )
    with expected, phase:
        if error is not None:
            raise error

    label = (
        "startup.snapshot 25.0ms" if startup else "action.spawn.snapshot 25.0ms operation=operation"
    )
    assert caplog.messages == [f"{label} result={result}"]


@pytest.mark.parametrize("broken", ["clock", "logger"])
def test_action_measurement_failure_cannot_mask_the_operation(monkeypatch, broken):
    def fail(*_args, **_kwargs):
        raise ValueError("measurement failed")

    if broken == "clock":
        monkeypatch.setattr(latency, "monotonic", fail)
    else:
        monkeypatch.setattr(latency.logger, "info", fail)
    error = RuntimeError("operation failed")
    with (
        pytest.raises(RuntimeError) as caught,
        latency.action_phase(ActionRecord("spawn", "target", "key"), "snapshot"),
    ):
        raise error
    assert caught.value is error
