"""Installed-version probe for the Vibe harness: display only, never a runtime."""

from __future__ import annotations

import subprocess

from theater.harness.contracts.runtime import RuntimeCompatibility, RuntimeProbeContext

from .version import parse_vibe_version

_POLICY = "vibe-harness-2.24.0-compatible"
# Per-mode facts, all verified against real wheels: the orchestrator config that
# honours VIBE_SESSION_LOGGING__SAVE_DIR ships in 2.24.0 (2.20.x and older still
# load the legacy VibeConfig and write outside the isolated domain); manual's
# ask profile replaces default in 2.24.1; --yolo exists since 2.17.0.
_FLOOR = (2, 24, 0)
#: The newest Vibe release line whose unified session store this reader
#: understands: 2.26.x writes store_format_minor 8 (vibe 376f6a33). The probe is
#: display-only, so launch planning enforces the same ceiling (launch.plan_launch);
#: bump the ceiling in the same commit as unified_store_types.STORE_FORMAT_MINOR —
#: the qualified range once outran the reader, and finished jobs waited forever on
#: stores no reader could open.
_CEILING = (2, 27, 0)


def vibe_qualified_range_text() -> str:
    """The human-readable qualified Vibe range, shared by the probe and launch."""
    floor = ".".join(str(part) for part in _FLOOR)
    ceiling = ".".join(str(part) for part in _CEILING)
    return f">={floor},<{ceiling}"


def vibe_store_format_ceiling() -> tuple[int, int, int]:
    """The newest Vibe version whose unified session store this reader can read.

    The probe is display-only; launch planning enforces this ceiling so a too-new
    writer is refused before the child runs instead of writing a store that no
    reader understands.
    """
    return _CEILING


def probe_vibe_compatibility(context: RuntimeProbeContext) -> RuntimeCompatibility:
    """The isolated transcript domain and env overrides need the 2.24.0 config."""
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
    supported = _FLOOR <= version < _CEILING
    return RuntimeCompatibility(
        supported=supported,
        policy=_POLICY,
        native_version=".".join(str(part) for part in version),
        reason=(
            None if supported else f"Vibe harness support requires {vibe_qualified_range_text()}"
        ),
    )


__all__ = [
    "probe_vibe_compatibility",
    "vibe_qualified_range_text",
    "vibe_store_format_ceiling",
]
