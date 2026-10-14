"""Bounded process-group execution for the OpenCode and Pi adapters."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path


def terminate_group(
    process: subprocess.Popen[str] | subprocess.Popen[bytes], *, timeout: float = 3.0
) -> None:
    """Stop the process and descendants in its private session, even after leader exit."""
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except (ProcessLookupError, PermissionError):
            break
        time.sleep(0.05)
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=1)


def run_group(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout: float,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Capture one command while bounding and cleaning up every descendant."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        terminate_group(process)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
