"""Régie logging, log pruning and latency tracing."""

from __future__ import annotations

import logging
import os
import stat
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
