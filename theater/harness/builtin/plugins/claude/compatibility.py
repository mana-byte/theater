"""Bounded read-only admission for Claude's optional asynchronous hooks."""

from __future__ import annotations

import re
import subprocess

from theater.harness.contracts.runtime import RuntimeCompatibility, RuntimeProbeContext

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?: \(Claude Code\))?")
_POLICY = "claude-hooks-2.1.202-compatible"


def probe_claude_hooks(context: RuntimeProbeContext) -> RuntimeCompatibility:
    """Check the stable CLI range; hook payload decoding is the schema gate."""
    try:
        result = subprocess.run(
            [context.binary or "claude", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return RuntimeCompatibility(
            supported=False, policy=_POLICY, reason="Claude version probe could not complete"
        )
    match = _VERSION.fullmatch(result.stdout.strip())
    if result.returncode != 0 or match is None:
        return RuntimeCompatibility(
            supported=False, policy=_POLICY, reason="Claude did not report a stable CLI version"
        )
    version = tuple(int(part) for part in match.groups())
    supported = (2, 1, 202) <= version < (3, 0, 0)
    return RuntimeCompatibility(
        supported=supported,
        policy=_POLICY,
        native_version=".".join(match.groups()),
        reason=None if supported else "Claude native hooks require >=2.1.202,<3",
    )
