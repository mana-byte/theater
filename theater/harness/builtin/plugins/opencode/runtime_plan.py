"""Read-only compatibility probe for the OpenCode native TUI controls."""

from __future__ import annotations

import re
import subprocess

from theater.harness.contracts.runtime import (
    RuntimeCompatibility,
    RuntimeProbeContext,
    RuntimeSessionOrder,
)

from .constants import MODELS_TIMEOUT

# The v1 ceiling is the sole release bump value. Historical model-free probes
# and byte-stable consumed surfaces support this range; approval needs a model.
OPENCODE_TUI_MIN_VERSION = (1, 17, 15)
OPENCODE_TUI_MAX_VERSION = (1, 19, 4)
OPENCODE_SERVER_MIN_VERSION = OPENCODE_TUI_MIN_VERSION
OPENCODE_SERVER_MAX_VERSION = OPENCODE_TUI_MAX_VERSION


def release_text(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


_LAST_V1_RELEASE = (
    OPENCODE_TUI_MAX_VERSION[0],
    OPENCODE_TUI_MAX_VERSION[1],
    OPENCODE_TUI_MAX_VERSION[2] - 1,
)
OPENCODE_V1_QUALIFIED_RANGE = (
    f"{release_text(OPENCODE_TUI_MIN_VERSION)}-{release_text(_LAST_V1_RELEASE)}"
)
OPENCODE_TUI_COMPATIBILITY_POLICY = f"opencode-tui-native-controls-{OPENCODE_V1_QUALIFIED_RANGE}"
OPENCODE_SERVER_COMPATIBILITY_POLICY = (
    f"opencode-server-native-controls-{OPENCODE_V1_QUALIFIED_RANGE}"
)

# 1.x prints a bare release; 2.x prints `opencode v2.0.18`, so a leading `v` is allowed.
_VERSION = re.compile(
    r"(?<![0-9A-Za-z.])v?(\d+)\.(\d+)\.(\d+)(?P<prerelease>-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\b"
)


def parse_opencode_version(output: str) -> tuple[int, int, int] | None:
    match = _VERSION.search(output)
    if match is None or match.group("prerelease") is not None:
        return None
    try:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


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
    rendered = release_text(version)
    if not OPENCODE_TUI_MIN_VERSION <= version < OPENCODE_TUI_MAX_VERSION:
        return RuntimeCompatibility(
            supported=False,
            policy=OPENCODE_TUI_COMPATIBILITY_POLICY,
            native_version=rendered,
            reason="OpenCode release is outside the native control compatibility range "
            f"(qualified releases are {OPENCODE_V1_QUALIFIED_RANGE})",
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


# Detached-server topology policy, deliberately NOT yet in MANIFEST: cutover needs
# send/abort/lineage parity with the TUI host. Pins the release and session-first ordering.
SERVER_SESSION_ORDER = RuntimeSessionOrder.SESSION_FIRST
#: 2.x server topology, verified end to end on 2.0.18 and model-free on stock
#: 2.0.6/2.0.12/2.0.17/2.0.20 (banner, Basic auth, /api/info, POST/GET /api/session
#: with location.directory, session_message list; the consumed-surface diffs between
#: adjacent tags are additive): `/api` routes with `{data}` bodies, the
#: `server listening on` banner, `--server <url> -s <id>` attach, and a per-lineage database.
OPENCODE_SERVER_V2_COMPATIBILITY_POLICY = "opencode-server-native-controls-2.0"
OPENCODE_SERVER_V2_MIN_VERSION = (2, 0, 6)
OPENCODE_SERVER_V2_MAX_VERSION = (2, 1, 0)
#: The serve banner the stock binary prints on stdout, parsed by
#: server_discovery.parse_server_stdout_endpoint.
SERVER_STDOUT_MAX_BYTES = 65_536


__all__ = [
    "OPENCODE_TUI_COMPATIBILITY_POLICY",
    "OPENCODE_TUI_MAX_VERSION",
    "OPENCODE_TUI_MIN_VERSION",
    "parse_opencode_version",
    "probe_opencode_compatibility",
]
