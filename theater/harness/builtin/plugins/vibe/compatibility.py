"""Installed-version probe for the Vibe harness: display only, never a runtime."""

from __future__ import annotations

import subprocess

from theater.harness.contracts.runtime import RuntimeCompatibility, RuntimeProbeContext

from .version import parse_vibe_version

# _FLOOR is the first release whose config honours VIBE_SESSION_LOGGING__SAVE_DIR.
# Earlier releases write outside the isolated domain; mode thresholds live in launch.
_FLOOR = (2, 24, 0)
#: The first Vibe release not yet proved compatible with this store reader.
#: Launch planning enforces the ceiling because the probe is display-only.
_CEILING = (3, 0, 1)
_POLICY = f"vibe-harness-{'.'.join(map(str, _FLOOR))}-compatible"


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
