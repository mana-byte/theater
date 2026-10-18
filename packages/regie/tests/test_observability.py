"""Régie logging, log pruning and latency tracing."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from contextlib import nullcontext
from pathlib import Path

import pytest
from regie import latency
from regie.controllers.actions import ActionRecord
from regie.observability import configure_logging, prune_regie_generations


def test_regie_log_is_private_and_does_not_follow_a_symlink(tmp_path: Path) -> None:
    log_path = tmp_path / "private" / "regie.log"
    handle = configure_logging(log_path)
    try:
        logging.getLogger("regie.test").warning("visible marker")
    finally:
        handle.close()

    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    assert "visible marker" in log_path.read_text()

    target = tmp_path / "target.txt"
    target.write_text("untouched")
    log_path.unlink()
    log_path.symlink_to(target)
    with pytest.raises(OSError):
        configure_logging(log_path)
    assert target.read_text() == "untouched"


def test_prune_regie_logs_preserves_current_live_and_three_newest_inactive_groups(
    tmp_path: Path,
) -> None:
    groups: dict[str, list[Path]] = {}
    for index, identity in enumerate(("pane-1", "pane-2", "pid-3", "pane-4", "pid-5", "pane-6"), 1):
        groups[identity] = [tmp_path / f"{identity}.log", tmp_path / f"{identity}.log.1"]
        for path in groups[identity]:
            path.write_text(path.name)
            os.utime(path, (index, index))
    unrelated = tmp_path / "bridge.log"
    unrelated.write_text("keep")

    deleted = prune_regie_generations(
        tmp_path,
        groups["pane-1"][0],
        protected=("%2",),
    )

    assert deleted == 2
    assert all(
        path.exists() for identity, paths in groups.items() if identity != "pid-3" for path in paths
    )
    assert all(not path.exists() for path in groups["pid-3"])
    assert unrelated.exists()


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


def test_startup_trace_flushes_preflight_once_and_includes_launcher_time(monkeypatch, caplog):
    caplog.set_level("INFO", logger="regie.latency")
    ticks = iter((10.0, 10.025, 11.0))
    monkeypatch.setattr(latency, "monotonic", lambda: next(ticks))
    trace = latency.StartupTrace(9.0)
    with trace.phase("daemon_preflight"):
        pass
    assert not caplog.messages
    trace.activate()
    trace.activate()
    latency.startup_milestone("ready", trace.started_at)
    assert caplog.messages == [
        "startup.daemon_preflight 25.0ms result=success",
        "startup.ready 2000.0ms",
    ]
