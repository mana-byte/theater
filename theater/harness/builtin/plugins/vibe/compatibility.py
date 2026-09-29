"""Installed-version probe for the Vibe harness: display only, never a runtime."""

from __future__ import annotations

import re
import subprocess

from theater.harness.contracts.runtime import RuntimeCompatibility, RuntimeProbeContext

_VERSION = re.compile(r"vibe (\d+)\.(\d+)\.(\d+)")
_POLICY = "vibe-harness-2.25.0-compatible"
_FLOOR = (2, 25, 0)
_CEILING = (3, 0, 0)


def probe_vibe_compatibility(context: RuntimeProbeContext) -> RuntimeCompatibility:
    """The ask agent profile (manual approval) first exists in 2.25.0."""
    try:
        result = subprocess.run(
            [context.binary or "vibe", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return RuntimeCompatibility(
            supported=False, policy=_POLICY, reason="Vibe version probe could not complete"
        )
    match = _VERSION.fullmatch(result.stdout.strip())
    if result.returncode != 0 or match is None:
        return RuntimeCompatibility(
            supported=False, policy=_POLICY, reason="Vibe did not report a stable CLI version"
        )
    version = tuple(int(part) for part in match.groups())
    supported = _FLOOR <= version < _CEILING
    range_text = f">={'.'.join(str(part) for part in _FLOOR)},<3"
    return RuntimeCompatibility(
        supported=supported,
        policy=_POLICY,
        native_version=".".join(match.groups()),
        reason=None if supported else f"Vibe harness support requires {range_text}",
    )


__all__ = ["probe_vibe_compatibility"]
