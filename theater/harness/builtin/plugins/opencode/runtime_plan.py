"""Read-only compatibility probe for the OpenCode native TUI controls."""

from __future__ import annotations

import re
import subprocess

from theater.harness.contracts.runtime import RuntimeCompatibility, RuntimeProbeContext

from .constants import MODELS_TIMEOUT

# Native control evidence is pinned to OpenCode 1.18.29 (upstream commit
# c470c79513f78aabb2ff88a8c8f7a3a22c4e97af): the shipped TUI plugin conformance
# fixture and the opt-in stock-UI gate prove request/response send, exact
# message lineage and epoch handling on that release and nothing newer.
OPENCODE_TUI_COMPATIBILITY_POLICY = "opencode-tui-native-controls-1.18.29"
OPENCODE_TUI_MIN_VERSION = (1, 18, 29)
OPENCODE_TUI_MAX_VERSION = (1, 18, 30)

_VERSION = re.compile(
    r"\b(\d+)\.(\d+)\.(\d+)(?P<prerelease>-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\b"
)


def parse_opencode_version(output: str) -> tuple[int, int, int] | None:
    match = _VERSION.search(output)
    if match is None or match.group("prerelease") is not None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def probe_opencode_compatibility(context: RuntimeProbeContext) -> RuntimeCompatibility:
    binary = context.binary or "opencode"
    try:
        version_run = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=MODELS_TIMEOUT,
            check=False,
        )
        help_run = subprocess.run(
            [binary, "--help"],
            capture_output=True,
            text=True,
            timeout=MODELS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _unsupported(f"could not run read-only OpenCode probes: {exc}")
    output = f"{version_run.stdout}\n{version_run.stderr}"
    version = parse_opencode_version(output)
    if version_run.returncode != 0 or version is None:
        return _unsupported("opencode --version did not report a usable release")
    rendered = ".".join(str(part) for part in version)
    if not OPENCODE_TUI_MIN_VERSION <= version < OPENCODE_TUI_MAX_VERSION:
        return RuntimeCompatibility(
            supported=False,
            policy=OPENCODE_TUI_COMPATIBILITY_POLICY,
            native_version=rendered,
            reason="OpenCode release is outside the native control compatibility range; "
            "conformance evidence exists for 1.18.29 only",
        )
    help_text = f"{help_run.stdout}\n{help_run.stderr}"
    if help_run.returncode != 0 or not {"--model", "--auto", "--fork"}.issubset(help_text.split()):
        return RuntimeCompatibility(
            supported=False,
            policy=OPENCODE_TUI_COMPATIBILITY_POLICY,
            native_version=rendered,
            reason="OpenCode help probe did not expose the stock launch options Theater preserves",
        )
    return RuntimeCompatibility(
        supported=True,
        policy=OPENCODE_TUI_COMPATIBILITY_POLICY,
        native_version=rendered,
    )


def _unsupported(reason: str) -> RuntimeCompatibility:
    return RuntimeCompatibility(
        supported=False,
        policy=OPENCODE_TUI_COMPATIBILITY_POLICY,
        reason=reason,
    )


__all__ = [
    "OPENCODE_TUI_COMPATIBILITY_POLICY",
    "OPENCODE_TUI_MAX_VERSION",
    "OPENCODE_TUI_MIN_VERSION",
    "parse_opencode_version",
    "probe_opencode_compatibility",
]
