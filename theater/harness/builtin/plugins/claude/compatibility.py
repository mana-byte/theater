"""Bounded read-only admission for Claude's optional asynchronous hooks."""

from __future__ import annotations

import re
import subprocess

from theater.harness.contracts.runtime import RuntimeCompatibility, RuntimeProbeContext

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?: \(Claude Code\))?")
_HOOK_FLOOR = (2, 1, 119)
#: Newest release the daily compat pipeline proved (inclusive); it is the only value it moves.
_QUALIFIED_MAX = (2, 1, 304)
_MESSAGING_FLOOR = (2, 1, 248)
_POLICY = f"claude-hooks-{'.'.join(map(str, _HOOK_FLOOR))}-compatible"
_MESSAGING_POLICY = f"claude-messaging-native-controls-{'.'.join(map(str, _MESSAGING_FLOOR))}"


def claude_messaging_range_text() -> str:
    return f">={'.'.join(map(str, _MESSAGING_FLOOR))}"


def probe_claude_hooks(context: RuntimeProbeContext) -> RuntimeCompatibility:
    """Check the qualified CLI range; hook payload decoding is the schema gate."""
    # The hook floor is where PostToolUse/PostToolUseFailure gained duration_ms.
    return _probe_claude(context, floor=_HOOK_FLOOR, policy=_POLICY, maximum=_QUALIFIED_MAX)


def probe_claude_native_compatibility(context: RuntimeProbeContext) -> RuntimeCompatibility:
    """Check the documented same-machine messaging floor."""
    return _probe_claude(context, floor=_MESSAGING_FLOOR, policy=_MESSAGING_POLICY)


def _probe_claude(
    context: RuntimeProbeContext,
    *,
    floor: tuple[int, int, int],
    policy: str,
    maximum: tuple[int, int, int] | None = None,
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
    supported = version >= floor and (maximum is None or version <= maximum)
    lower_text = ".".join(str(part) for part in floor)
    range_text = (
        f">={lower_text}"
        if maximum is None
        else f">={lower_text},<={'.'.join(str(part) for part in maximum)}"
    )
    return RuntimeCompatibility(
        supported=supported,
        policy=policy,
        native_version=".".join(match.groups()),
        reason=None if supported else f"Claude native support requires {range_text}",
    )


__all__ = ["probe_claude_hooks", "probe_claude_native_compatibility"]
