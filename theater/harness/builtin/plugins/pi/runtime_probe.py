"""Compatibility probing for the Pi frontend runtime."""

from __future__ import annotations

import subprocess

from theater.harness.contracts.runtime import RuntimeCompatibility, RuntimeProbeContext

from .constants import PI_BINARY
from .runtime_constants import (
    _VERSION_TOKEN,
    PI_FRONTEND_QUALIFIED_EXCLUDED,
    PI_FRONTEND_QUALIFIED_FLOOR,
    PI_FRONTEND_QUALIFIED_MAX,
    PI_FRONTEND_QUALIFIED_RANGE,
    PI_FRONTEND_RUNTIME_COMPATIBILITY_POLICY,
    PI_FRONTEND_RUNTIME_PROBE_TIMEOUT_SECONDS,
)


def parse_pi_version(output: str) -> str | None:
    """Extract a Pi release version from ``pi --version``."""
    match = _VERSION_TOKEN.search(output)
    if match is None or int(match.group("major")) > PI_FRONTEND_QUALIFIED_MAX[0]:
        return None
    return match.group("version")


def _version_in_supported_range(version: str) -> bool:
    match = _VERSION_TOKEN.fullmatch(version)
    if match is None:
        return False
    try:
        parsed = tuple(int(match.group(part)) for part in ("major", "minor", "patch"))
    except ValueError:
        return False
    # Excluded releases are broken published artifacts inside the range.
    if parsed in PI_FRONTEND_QUALIFIED_EXCLUDED:
        return False
    return PI_FRONTEND_QUALIFIED_FLOOR <= parsed <= PI_FRONTEND_QUALIFIED_MAX


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
        release = (
            f"{PI_FRONTEND_QUALIFIED_MAX[0]}.x.y release"
            if PI_FRONTEND_QUALIFIED_MAX[0] == 0
            else "release"
        )
        return _unsupported_probe(f"Pi --version did not report a stable {release}")
    if not _version_in_supported_range(version):
        return _unsupported_probe(
            f"Pi {version} is outside the qualified range {PI_FRONTEND_QUALIFIED_RANGE} "
            "(excluded releases are broken published artifacts)",
            version=version,
        )
    return RuntimeCompatibility(
        supported=True,
        policy=PI_FRONTEND_RUNTIME_COMPATIBILITY_POLICY,
        native_version=version,
    )
