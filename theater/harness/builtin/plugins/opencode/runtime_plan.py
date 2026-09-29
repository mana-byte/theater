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

# 1.17.15-1.18.33 qualified by byte-stable Theater-consumed surfaces plus
# model-free live probes on stock 1.17.15/1.17.20/1.18.0/1.18.10/1.18.28; the paid
# model-backed conformance suite still pins 1.18.29 (upstream
# c470c79513f78aabb2ff88a8c8f7a3a22c4e97af), and 1.x approval enforcement
# (the plugin's chat.message hook) is not model-free verifiable.
OPENCODE_TUI_COMPATIBILITY_POLICY = "opencode-tui-native-controls-1.17.15-1.18.33"
OPENCODE_TUI_MIN_VERSION = (1, 17, 15)
OPENCODE_TUI_MAX_VERSION = (1, 18, 34)

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
    rendered = ".".join(str(part) for part in version)
    if not OPENCODE_TUI_MIN_VERSION <= version < OPENCODE_TUI_MAX_VERSION:
        return RuntimeCompatibility(
            supported=False,
            policy=OPENCODE_TUI_COMPATIBILITY_POLICY,
            native_version=rendered,
            reason="OpenCode release is outside the native control compatibility range "
            "(qualified releases are 1.17.15-1.18.33)",
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
OPENCODE_SERVER_COMPATIBILITY_POLICY = "opencode-server-native-controls-1.17.15-1.18.33"
OPENCODE_SERVER_MIN_VERSION = (1, 17, 15)
OPENCODE_SERVER_MAX_VERSION = (1, 18, 34)
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
