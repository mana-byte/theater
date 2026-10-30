"""Vibe version bounds: a display-only probe; launch planning enforces both bounds."""

from __future__ import annotations

import subprocess
from pathlib import Path

from theater.harness.contracts import version_probe
from theater.harness.contracts.runtime import RuntimeCompatibility, RuntimeProbeContext
from theater.harness.contracts.version_policy import VersionPolicy, version_key

from .version import _VERSION

VERSION_POLICY = VersionPolicy.load(Path(__file__).with_name("compatibility.json"))
_POLICY = f"vibe-harness-{VERSION_POLICY.minimum}-compatible"


def vibe_qualified_range_text() -> str:
    """The human-readable qualified Vibe range the manifest advertises."""
    return VERSION_POLICY.range_text


def probe_vibe_compatibility(context: RuntimeProbeContext) -> RuntimeCompatibility:
    """The isolated transcript domain and env overrides need the floor's config."""
    try:
        version = version_probe.probe_release(context.binary or "vibe", _VERSION, timeout=5)
    except subprocess.CalledProcessError:
        version = None
    except (OSError, subprocess.SubprocessError):
        return RuntimeCompatibility(
            supported=False, policy=_POLICY, reason="Vibe version probe could not complete"
        )
    if version is not None:
        try:
            version_key(version)
        except ValueError:
            version = None
    if version is None:
        return RuntimeCompatibility(
            supported=False, policy=_POLICY, reason="Vibe did not report a stable CLI version"
        )
    supported = VERSION_POLICY.admits(version)
    return RuntimeCompatibility(
        supported=supported,
        policy=_POLICY,
        native_version=version,
        reason=VERSION_POLICY.refusal("Vibe", version),
    )


__all__ = [
    "probe_vibe_compatibility",
    "vibe_qualified_range_text",
]
