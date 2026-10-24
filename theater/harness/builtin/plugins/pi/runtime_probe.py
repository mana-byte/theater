"""Compatibility probing for the Pi frontend runtime."""

from __future__ import annotations

import subprocess

from theater.harness.contracts import version_probe
from theater.harness.contracts.runtime import RuntimeCompatibility, RuntimeProbeContext
from theater.harness.contracts.version_policy import version_key

from .constants import PI_BINARY
from .runtime_constants import (
    _VERSION_TOKEN,
    PI_FRONTEND_QUALIFIED_RANGE,
    PI_FRONTEND_RUNTIME_COMPATIBILITY_POLICY,
    PI_FRONTEND_RUNTIME_PROBE_TIMEOUT_SECONDS,
    VERSION_POLICY,
)


def _unsupported_probe(reason: str, *, version: str | None = None) -> RuntimeCompatibility:
    return RuntimeCompatibility(
        supported=False,
        policy=PI_FRONTEND_RUNTIME_COMPATIBILITY_POLICY,
        native_version=version,
        reason=f"{reason}; install a qualified release or upgrade Theater",
    )


def probe_pi_frontend_compatibility(context: RuntimeProbeContext) -> RuntimeCompatibility:
    """Run only read-only executable/CLI-surface checks for the Pi bridge.

    Not ``pi --help``: stock Pi can touch its settings lock while rendering help.
    """
    binary = context.binary or PI_BINARY
    try:
        version = version_probe.probe_release(
            binary, _VERSION_TOKEN, timeout=PI_FRONTEND_RUNTIME_PROBE_TIMEOUT_SECONDS
        )
    except subprocess.CalledProcessError as exc:
        return _unsupported_probe(f"Pi --version exited with {exc.returncode}")
    except (OSError, subprocess.SubprocessError) as exc:
        return _unsupported_probe(
            f"Pi compatibility probe could not run {binary!r} --version: {exc}"
        )
    if version is None:
        return _unsupported_probe("Pi --version did not report a stable release")
    try:
        version_key(version)
    except ValueError:
        return _unsupported_probe("Pi --version did not report a stable release")
    if not VERSION_POLICY.admits(version):
        return _unsupported_probe(
            f"Pi {version} is outside the qualified range {PI_FRONTEND_QUALIFIED_RANGE} "
            "(excluded releases did not pass qualification)",
            version=version,
        )
    return RuntimeCompatibility(
        supported=True,
        policy=PI_FRONTEND_RUNTIME_COMPATIBILITY_POLICY,
        native_version=version,
    )
