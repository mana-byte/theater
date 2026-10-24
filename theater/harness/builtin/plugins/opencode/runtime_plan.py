"""OpenCode's stable release policy and CLI version parsing."""

from __future__ import annotations

import re
from pathlib import Path

from theater.harness.contracts.version_policy import VersionPolicy, version_key

VERSION_POLICY = VersionPolicy.load(Path(__file__).with_name("compatibility.json"))
OPENCODE_SERVER_COMPATIBILITY_POLICY = (
    f"opencode-server-native-controls-{VERSION_POLICY.minimum}-compatible"
)
SERVER_STDOUT_MAX_BYTES = 65_536
_VERSION = re.compile(r"\A(?:opencode[ \t]+)?v?(\S+)\Z")


def release_text(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def parse_opencode_version(output: str) -> tuple[int, int, int] | None:
    """Parse the stock banner or a server's bare canonical release."""
    match = _VERSION.fullmatch(output.strip())
    if match is None:
        return None
    try:
        return version_key(match[1])
    except ValueError:
        return None
