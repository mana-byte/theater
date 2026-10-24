"""Installed Vibe version detection, shared by the probe and the launch planner."""

from __future__ import annotations

import re
import shutil
import subprocess

from theater.harness.contracts import version_probe
from theater.harness.contracts.version_policy import version_key

_VERSION = re.compile(r"\Avibe (\S+)\Z")


def parse_vibe_version(text: str) -> tuple[int, int, int] | None:
    """The one parser for ``vibe --version`` output; ``None`` when unrecognised."""
    match = _VERSION.fullmatch(text.strip())
    if match is None:
        return None
    try:
        return version_key(match[1])
    except ValueError:
        return None


def resolve_vibe_binary(binary: str | None = None) -> str | None:
    """The absolute path the daemon's PATH finds, or ``None`` when it does not.

    Launch argv[0] uses this so the pane runs the exact binary the version probe
    resolved; the pane's PATH may point at a different install.
    """
    return shutil.which(binary or "vibe")


def installed_vibe_version(binary: str | None = None) -> tuple[int, int, int] | None:
    """The verified release on PATH, cached by executable identity."""
    try:
        version = version_probe.probe_release(binary or "vibe", _VERSION, timeout=5)
        return version_key(version) if version is not None else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


__all__ = ["installed_vibe_version", "parse_vibe_version", "resolve_vibe_binary"]
