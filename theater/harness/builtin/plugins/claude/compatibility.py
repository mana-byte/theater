"""Read-only admission shared by Claude launches, hooks and native display."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from theater.harness.contracts.runtime import RuntimeCompatibility, RuntimeProbeContext
from theater.harness.contracts.version_policy import VersionPolicy, version_key

VERSION_POLICY = VersionPolicy.load(Path(__file__).with_name("compatibility.json"))
_VERSION = re.compile(r"(\S+)(?: \(Claude Code\))?")
_POLICY = f"claude-hooks-{VERSION_POLICY.minimum}-compatible"
_MESSAGING_POLICY = f"claude-messaging-native-controls-{VERSION_POLICY.minimum}"


def claude_messaging_range_text() -> str:
    return VERSION_POLICY.range_text


def probe_claude_hooks(context: RuntimeProbeContext) -> RuntimeCompatibility:
    """Check the qualified CLI range; hook payload decoding is the schema gate."""
    return _probe_claude(context, policy=_POLICY)


def probe_claude_native_compatibility(context: RuntimeProbeContext) -> RuntimeCompatibility:
    """Check the plugin's qualified CLI range."""
    return _probe_claude(context, policy=_MESSAGING_POLICY)


def _probe_claude(
    context: RuntimeProbeContext,
    *,
    policy: str,
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
    version = match[1]
    try:
        version_key(version)
    except ValueError:
        return RuntimeCompatibility(
            supported=False, policy=policy, reason="Claude did not report a stable CLI version"
        )
    supported = VERSION_POLICY.admits(version)
    return RuntimeCompatibility(
        supported=supported,
        policy=policy,
        native_version=version,
        reason=None if supported else f"Claude support requires {VERSION_POLICY.range_text}",
    )


__all__ = ["probe_claude_hooks", "probe_claude_native_compatibility"]
