"""Run the mutation canaries against the newest admitted release of one harness.

A canary breaks a plugin's wiring on purpose (``harness_compat/support/mutations.py``); the live
suite must fail the obligations it breaks. They prove the suite still has teeth, so they run when
the suite changes and weekly, never per release. The canary tests run in a disposable code copy
that admits only the chosen release, and each scenario copies that copy again: nothing runs in the
checkout.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from theater.version_policy import version_key

from ..contract import Adapter, Check, InstallFailed
from ..diagnostics import clip_diagnostic
from ..policy import admit_only, load_policy
from .codecopy import code_copy
from .framework import python_path
from .process import run_group
from .qualify import install_verified
from .sandbox import make_sandbox

CANARIES_TARGET = "harness_compat/conformance/canaries"
CANARIES_TIMEOUT_SECONDS = 45 * 60.0
#: Read by the canary tests: opt in, and the one harness and installed binary to run against.
ENV_ENABLE = "HARNESS_COMPAT_CANARIES"
ENV_HARNESS = "HARNESS_COMPAT_CANARY_HARNESS"
ENV_BINARY = "HARNESS_COMPAT_CANARY_BINARY"
_PARALLEL_WORKERS = "2"
_TALLY = re.compile(r"(\d+) (passed|failed|skipped|error)")


def canary_version(root: Path, adapter: Adapter) -> str:
    """Use the newest admitted release, including when max has been excluded."""
    policy = load_policy(root, adapter.name)
    if policy.admits(policy.maximum):
        return policy.maximum
    admitted = [version for version in adapter.discover() if policy.admits(version)]
    if not admitted:
        raise ValueError(f"{adapter.name} has no admitted release for suite canaries")
    return max(admitted, key=version_key)


def run_canaries(
    root: Path,
    adapter: Adapter,
    *,
    version: str | None = None,
    timeout: float = CANARIES_TIMEOUT_SECONDS,
) -> Check:
    """pass: every applicable canary was caught; fail: the suite missed one; error: no verdict."""
    chosen = version or canary_version(root, adapter)
    with make_sandbox(adapter.name) as sb:
        try:
            binary = install_verified(adapter, sb, chosen)
        except (InstallFailed, OSError, subprocess.SubprocessError) as exc:
            return Check("canaries", "error", f"{adapter.name} {chosen}: {_tail(str(exc))}")
        with code_copy(root) as tree:
            if not load_policy(tree, adapter.name).admits(chosen):
                admit_only(tree, adapter.name, chosen)
            env = {
                **sb.offline_env,
                ENV_ENABLE: "1",
                ENV_HARNESS: adapter.name,
                ENV_BINARY: str(binary),
                "PYTHONPATH": python_path(tree),
                "PYTHONDONTWRITEBYTECODE": "1",
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
                "--tb=line",
                CANARIES_TARGET,
            ]
            run = run_group(argv, cwd=tree, env=env, timeout=timeout)
    tally = {kind: int(count) for count, kind in _TALLY.findall(run.stdout)}
    shown = f"{adapter.name} {chosen}: " + ", ".join(f"{n} {kind}" for kind, n in tally.items())
    if run.returncode == 0 and tally.get("passed", 0) > 0:
        return Check("canaries", "pass", shown)
    if run.returncode == 1:
        # One line per failure (its assertion message names what the suite did not notice).
        return Check("canaries", "fail", f"{shown}; {clip_diagnostic(run.stdout, 1500, tail=True)}")
    if run.returncode == 0:
        return Check("canaries", "error", f"{shown}; no canary applied to this harness")
    return Check(
        "canaries", "error", f"pytest exit {run.returncode}: {_tail(run.stdout + run.stderr)}"
    )


def _tail(text: str) -> str:
    return clip_diagnostic(text, 400, tail=True)
