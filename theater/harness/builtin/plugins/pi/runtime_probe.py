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
    # Qualified: (0,80,8) <= v < (0,85,0) or (0,85,1) <= v <= (0,99,1).
    # 0.85.0 is a broken published artifact (unresolvable pi-server import);
    # 0.80.3-0.80.7 never qualify: their SDK crashes at import and lacks
    # ModelRuntime and agent_settled.
    in_080 = (0, 80, 8) <= parsed < (0, 85, 0)
    in_qualified = (0, 85, 1) <= parsed <= (0, 99, 1)
    return in_080 or in_qualified


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
            f"Pi {version} is outside the qualified range "
            ">=0.80.8,<0.85.0 || >=0.85.1,<=0.99.1 "
            "(0.85.0 is a broken published artifact)",
            version=version,
        )
    return RuntimeCompatibility(
        supported=True,
        policy=PI_FRONTEND_RUNTIME_COMPATIBILITY_POLICY,
        native_version=version,
    )
