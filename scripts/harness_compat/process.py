"""Run subprocesses in their own session and reap the whole group on every exit path."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

_GRACE_SECONDS = 5.0


def run_group(
    argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """Like subprocess.run, but a timeout or error kills the child's whole process group."""
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        terminate_group(process)
        raise
    terminate_group(process)  # stragglers the child left behind
    return subprocess.CompletedProcess(list(argv), process.returncode, stdout, stderr)


def terminate_group(process: subprocess.Popen[str]) -> None:
    """SIGTERM then SIGKILL the group led by ``process``; always reap the leader."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_GRACE_SECONDS)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()
