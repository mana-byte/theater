"""Vibe version bounds: a display-only probe; launch planning enforces both bounds."""

from __future__ import annotations

import subprocess
from pathlib import Path

from theater.harness.contracts.runtime import RuntimeCompatibility, RuntimeProbeContext
from theater.harness.contracts.version_policy import VersionPolicy

from .version import parse_vibe_version

VERSION_POLICY = VersionPolicy.load(Path(__file__).with_name("compatibility.json"))
_POLICY = f"vibe-harness-{VERSION_POLICY.minimum}-compatible"


def vibe_qualified_range_text() -> str:
    """The human-readable qualified Vibe range, shared by the probe and launch."""
    return VERSION_POLICY.range_text


def probe_vibe_compatibility(context: RuntimeProbeContext) -> RuntimeCompatibility:
    """The isolated transcript domain and env overrides need the floor's config."""
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
    version = parse_vibe_version(result.stdout) if result.returncode == 0 else None
    if version is None:
        return RuntimeCompatibility(
            supported=False, policy=_POLICY, reason="Vibe did not report a stable CLI version"
        )
    rendered = ".".join(str(part) for part in version)
    supported = VERSION_POLICY.admits(rendered)
    return RuntimeCompatibility(
        supported=supported,
        policy=_POLICY,
        native_version=rendered,
        reason=(
            None if supported else f"Vibe harness support requires {vibe_qualified_range_text()}"
        ),
    )


__all__ = [
    "probe_vibe_compatibility",
    "vibe_qualified_range_text",
]
