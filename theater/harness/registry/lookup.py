"""Registry lookup: normalize, get, observation_lookup, icon, describe, known binaries."""

from __future__ import annotations

import shutil
from typing import Literal

from theater.harness.contracts.harness import Harness
from theater.harness.contracts.runtime import (
    RuntimeCapability,
    RuntimeCompatibility,
    RuntimeCompatibilityProbe,
    RuntimeManifest,
)
from theater.harness.registry import (
    _ALIASES,
    _BROKEN,
    _OBSERVATION_KEYS,
    _PLUGINS,
    HARNESSES,
)
from theater.harness.registry.diagnostics import HarnessRuntimeHealth, project_plugin
from theater.models import BadRequest

#: Shown for a participant whose harness has no adapter.
UNKNOWN_ICON = "?"

type NativeCompatibilityStatus = Literal[
    "native-compatible",
    "outside-qualified-range",
    "legacy-only",
    "not-installed",
    "unknown",
]


def normalize(name: str) -> str:
    """Map a harness name as an agent might report it to the canonical key.

    Unknown names are returned unchanged so the caller can decide whether to
    reject or accept as-is.
    """
    return _ALIASES.get(name, name)


def get(name: str) -> Harness:
    """Return the harness for ``name``, or raise ``BadRequest``."""
    harness = HARNESSES.get(name)
    if harness is None:
        known = ", ".join(sorted(HARNESSES))
        raise BadRequest(f"unknown harness {name!r}; known: {known}")
    return harness


def observation_lookup(key: str) -> str | None:
    """Resolve a 15-character observation to a harness name, or None.

    15 is the truncation of tmux ``pane_current_command`` and Linux ``/proc/<pid>/comm``.
    """
    entry = _OBSERVATION_KEYS.get(key)
    return entry[0] if entry is not None else None


def harness_icon(name: str | None) -> str:
    """The one-character mark for a harness name.

    Normalizes first so aliases receive their canonical glyph. Unknown names
    are not an error here.
    """
    harness = HARNESSES.get(normalize(name or ""))
    return harness.icon if harness else UNKNOWN_ICON


def native_compatibility_probe(harness: Harness) -> RuntimeCompatibilityProbe | None:
    """Return the declared display probe, falling back to the runtime probe."""
    declared = harness.native_compatibility
    if declared is not None:
        return declared.probe
    return None if harness.runtime is None else harness.runtime.probe


#: Shown instead of a range that qualifies nothing: no runtime means no native route.
_RUNTIMELESS_REASON = "no native runtime; controls use the provider terminal"
_PROBE_FAILURE_LIMIT = 200

#: One probe attempt through the daemon's ``harnesses`` flow: the answer, the
#: error that prevented one, or nothing when the harness declares no probe.
type ProbeOutcome = RuntimeCompatibility | Exception | None


def _probe_failure_reason(error: Exception) -> str:
    """Bounded one-line cause for a probe that could not run."""
    return f"compatibility probe failed: {type(error).__name__}: {error}"[:_PROBE_FAILURE_LIMIT]


def _native_routes(manifest: RuntimeManifest) -> dict[str, str]:
    """Manifest-derived routes; mirrors ``manifest_control_routes`` (daemon/controls)."""
    return {
        capability.value: (
            "tmux"
            if capability in manifest.legacy_fallback
            else "unavailable"
            if capability in manifest.unavailable_capabilities
            else "native"
        )
        for capability in RuntimeCapability
    }


def _legacy_routes(harness: Harness) -> dict[str, str]:
    """Pane-wired routes; mirrors ``_legacy_capabilities`` (daemon/rpc/controls)."""
    controls = getattr(harness, "controls", None)
    interrupt_plan = None if controls is None else getattr(controls, "interrupt", None)
    return {
        RuntimeCapability.SEND.value: "tmux",
        RuntimeCapability.STEER.value: "unavailable",
        RuntimeCapability.QUEUE_FOLLOWUP.value: "tmux",
        RuntimeCapability.SETTINGS_UPDATE.value: "unavailable",
        RuntimeCapability.INTERRUPT.value: "tmux" if interrupt_plan is not None else "unavailable",
    }


def native_compatibility_record(
    harness: Harness,
    *,
    installed: bool,
    result: RuntimeCompatibility | None = None,
    error: Exception | None = None,
) -> dict[str, object]:
    """Build the stable daemon/UI view of one harness's native qualification."""
    declared = harness.native_compatibility
    qualified_range = None if declared is None else declared.qualified_range
    probe = native_compatibility_probe(harness)
    runtimeless = harness.runtime is None
    if not installed:
        status: NativeCompatibilityStatus = "not-installed"
    elif runtimeless:
        status = "legacy-only"
    elif error is not None:
        status = "unknown"
    elif probe is None:
        status = "legacy-only"
    elif result is None or (not result.supported and result.native_version is None):
        status = "unknown"
    elif not result.supported:
        status = "outside-qualified-range"
    else:
        status = "native-compatible"
    reason = None if result is None else result.reason
    if runtimeless and installed:
        qualified_range = None
        reason = _RUNTIMELESS_REASON
    if error is not None:
        reason = _probe_failure_reason(error)
    wiring = (
        None
        if status == "not-installed"
        else ("native" if status == "native-compatible" else "legacy")
    )
    routes: dict[str, str] | None = None
    if wiring == "native" and harness.runtime is not None:
        routes = _native_routes(harness.runtime)
    elif wiring == "legacy":
        routes = _legacy_routes(harness)
    return {
        "status": status,
        "installed_version": None if result is None else result.native_version,
        "qualified_range": qualified_range,
        "policy": None if result is None else result.policy,
        "reason": reason,
        "wiring": wiring,
        "routes": routes,
    }


def describe(*, runtime: HarnessRuntimeHealth | None = None) -> list[dict]:
    """Every registered harness as plain data, sorted by name.

    ``installed`` reflects the calling process's PATH; broken local plugins come last.
    """
    rows = []
    for name in sorted(HARNESSES):
        harness = HARNESSES[name]
        path = shutil.which(harness.binary)
        rows.append(
            project_plugin(
                {
                    "name": name,
                    "icon": harness.icon,
                    "binary": harness.binary,
                    "binaries": sorted(harness.binaries),
                    "installed": path is not None,
                    "path": path,
                    "source": _PLUGINS[name].source,
                    "approvals": list(harness.launch_parameter_support.approvals),
                    "error": None,
                },
                _PLUGINS[name],
                runtime,
            )
        )
    for found in sorted(_BROKEN, key=lambda p: p.name):
        rows.append(
            {
                "name": found.name,
                "icon": UNKNOWN_ICON,
                "binary": "",
                "binaries": [],
                "installed": False,
                "path": str(found.path),
                "source": found.source,
                "error": found.error,
            }
        )
    return rows


def known_binaries() -> set[str]:
    """Every binary name the registered harnesses look for on PATH.

    Used by the unmanaged-pane sweep. Includes plugin-declared ``binaries``
    aliases when set.
    """
    result: set[str] = set()
    for h in HARNESSES.values():
        result.add(h.binary)
        result |= h.binaries
    return result
