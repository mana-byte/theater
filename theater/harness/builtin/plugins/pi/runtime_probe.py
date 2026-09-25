"""Compatibility probing for the Pi frontend runtime."""

from __future__ import annotations

import subprocess

from theater.harness.contracts.runtime import RuntimeCompatibility, RuntimeProbeContext

from .constants import PI_BINARY
from .runtime_constants import (
    _VERSION_TOKEN,
    PI_FRONTEND_RUNTIME_COMPATIBILITY_POLICY,
    PI_FRONTEND_RUNTIME_PROBE_TIMEOUT_SECONDS,
)


def parse_pi_version(output: str) -> str | None:
    """Extract a stable ``0.x.y`` Pi release version from ``pi --version``."""
    match = _VERSION_TOKEN.search(output)
    return None if match is None else match.group("version")


def _version_in_supported_range(version: str) -> bool:
    match = _VERSION_TOKEN.fullmatch(version)
    if match is None:
        return False
    try:
        parsed = (0, int(match.group("minor")), int(match.group("patch")))
    except ValueError:
        return False
    return (0, 84, 4) <= parsed < (0, 85, 0)


def _unsupported_probe(reason: str, *, version: str | None = None) -> RuntimeCompatibility:
    return RuntimeCompatibility(
        supported=False,
        policy=PI_FRONTEND_RUNTIME_COMPATIBILITY_POLICY,
        native_version=version,
        reason=(
            f"{reason}; Pi keeps its existing legacy launch and controls until a compatible "
            "frontend bridge is available"
        ),
    )


def probe_pi_frontend_compatibility(context: RuntimeProbeContext) -> RuntimeCompatibility:
    """Run only read-only executable/CLI-surface checks for the Pi bridge.

    Not ``pi --help``: stock Pi can touch its settings lock while rendering help.
    """
    binary = context.binary or PI_BINARY
    try:
        version_result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=PI_FRONTEND_RUNTIME_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _unsupported_probe(
            f"Pi compatibility probe could not run {binary!r} --version: {exc}"
        )
    if version_result.returncode != 0:
        return _unsupported_probe(f"Pi --version exited with {version_result.returncode}")
    version = parse_pi_version(f"{version_result.stdout}\n{version_result.stderr}")
    if version is None:
        return _unsupported_probe("Pi --version did not report a stable 0.x.y release")
    if not _version_in_supported_range(version):
        return _unsupported_probe(
            f"Pi {version} is outside supported range >=0.84.4,<0.85.0", version=version
        )
    return RuntimeCompatibility(
        supported=True,
        policy=PI_FRONTEND_RUNTIME_COMPATIBILITY_POLICY,
        native_version=version,
    )
