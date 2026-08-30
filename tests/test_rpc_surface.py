"""The daemon's public wire-method contract."""

from __future__ import annotations

import json
import subprocess
import sys

EXPECTED_RPC_METHODS = {
    "adopt",
    "bus.tail",
    "gc",
    "harnesses",
    "harness.event",
    "hello",
    "jobs.await",
    "jobs.status",
    "models",
    "participant.kill",
    "participant.interrupt",
    "participant.rename",
    "participant.update",
    "participant.status",
    "participants.get",
    "participants.list",
    "participants.recent_dead",
    "participants.tree",
    "participants.unmanaged",
    "ping",
    "read_transcript",
    "recall",
    "recall_read",
    "scratchpad.get",
    "scratchpad.write",
    "send",
    "skills.list",
    "skills.load",
    "shutdown",
    "spawn",
    "stats",
    "trajectory.close",
    "trajectory.follow",
    "trajectory.locate",
    "trajectory.snapshot",
    "transcript.bind",
    "transcript.candidates",
    "transcript.receipt",
    "usage_by_harness",
    "usage_summary",
    "usage_totals",
}

_PROBE = "import json, theater.daemon.server as server; print(json.dumps(sorted(server.METHODS)))"


def test_the_daemon_exposes_exactly_these_rpc_methods(tmp_path):
    """A cold production import must register every RPC handler."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert set(json.loads(result.stdout)) == EXPECTED_RPC_METHODS
