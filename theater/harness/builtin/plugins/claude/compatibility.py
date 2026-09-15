"""Bounded read-only admission for Claude's optional asynchronous hooks."""

from __future__ import annotations

import re
import subprocess

from theater.harness.contracts.runtime import RuntimeCompatibility, RuntimeProbeContext

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?: \(Claude Code\))?")
_POLICY = "claude-hooks-2.1.202-compatible"
_MESSAGING_POLICY = "claude-messaging-native-controls-2.1.248"


def probe_claude_hooks(context: RuntimeProbeContext) -> RuntimeCompatibility:
    """Check the stable CLI range; hook payload decoding is the schema gate."""
    return _probe_claude(context, floor=(2, 1, 202), policy=_POLICY, upper=(3, 0, 0))


def probe_claude_native_compatibility(context: RuntimeProbeContext) -> RuntimeCompatibility:
    """Check the documented same-machine messaging floor."""
    return _probe_claude(context, floor=(2, 1, 248), policy=_MESSAGING_POLICY)


def _probe_claude(
    context: RuntimeProbeContext,
    *,
    floor: tuple[int, int, int],
    policy: str,
    upper: tuple[int, int, int] | None = None,
) -> RuntimeCompatibility:
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
            supported=False, policy=policy, reason="Claude version probe could not complete"
        )
    match = _VERSION.fullmatch(result.stdout.strip())
    if result.returncode != 0 or match is None:
        return RuntimeCompatibility(
            supported=False, policy=policy, reason="Claude did not report a stable CLI version"
        )
    version = tuple(int(part) for part in match.groups())
    supported = version >= floor and (upper is None or version < upper)
    lower_text = ".".join(str(part) for part in floor)
    range_text = f">={lower_text}" if upper is None else f">={lower_text},<3"
    return RuntimeCompatibility(
        supported=supported,
        policy=policy,
        native_version=".".join(match.groups()),
        reason=None if supported else f"Claude native support requires {range_text}",
    )


__all__ = ["probe_claude_hooks", "probe_claude_native_compatibility"]
