"""Run the mutation canaries against the newest admitted release of one harness.

A canary breaks a plugin's wiring on purpose (``tests/support/mutations.py``); the live suite must
fail the obligations it breaks. They prove the suite still has teeth, so they run when the suite
changes and weekly, never per release.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from .contract import Adapter, Check
from .diagnostics import clip_diagnostic
from .process import run_group
from .sandbox import make_sandbox
from .select import newest_in_range

CANARIES_TARGET = "tests/conformance/canaries"
CANARIES_TIMEOUT_SECONDS = 45 * 60.0
#: Read by the canary tests: opt in, and the one harness and installed binary to run against.
ENV_ENABLE = "HARNESS_COMPAT_CANARIES"
ENV_HARNESS = "HARNESS_COMPAT_CANARY_HARNESS"
ENV_BINARY = "HARNESS_COMPAT_CANARY_BINARY"
_PARALLEL_WORKERS = "2"
_TALLY = re.compile(r"(\d+) (passed|failed|skipped|error)")


def run_canaries(
    root: Path,
    adapter: Adapter,
    *,
    version: str | None = None,
    timeout: float = CANARIES_TIMEOUT_SECONDS,
) -> Check:
    """pass: every applicable canary was caught; fail: the suite missed one; error: no verdict."""
    gate = adapter.verified(root)
    chosen = version or newest_in_range(adapter, gate)
    if chosen is None:
        return Check("canaries", "error", f"no stable {adapter.name} release is admitted")
    with make_sandbox(adapter.name) as sb:
        binary = adapter.install(sb, chosen)
        env = {
            **sb.offline_env,
            ENV_ENABLE: "1",
            ENV_HARNESS: adapter.name,
            ENV_BINARY: str(binary),
            "PYTHONPATH": str(root),
        }
        argv = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-n",
            _PARALLEL_WORKERS,
            "-k",
            adapter.name,
            CANARIES_TARGET,
        ]
        run = run_group(argv, cwd=root, env=env, timeout=timeout)
    tally = {kind: int(count) for count, kind in _TALLY.findall(run.stdout)}
    shown = f"{adapter.name} {chosen}: " + ", ".join(f"{n} {kind}" for kind, n in tally.items())
    if run.returncode == 0 and tally.get("passed", 0) > 0:
        return Check("canaries", "pass", shown)
    if run.returncode == 1:
        return Check("canaries", "fail", f"{shown}; {_tail(run.stdout)}")
    if run.returncode == 0:
        return Check("canaries", "error", f"{shown}; no canary applied to this harness")
    return Check(
        "canaries", "error", f"pytest exit {run.returncode}: {_tail(run.stdout + run.stderr)}"
    )


def _tail(text: str) -> str:
    return clip_diagnostic(text, 400, tail=True)
