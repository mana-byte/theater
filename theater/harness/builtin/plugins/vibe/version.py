"""Installed Vibe version detection, shared by the probe and the launch planner."""

from __future__ import annotations

import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

_VERSION = re.compile(r"vibe (\d+)\.(\d+)\.(\d+)")


def parse_vibe_version(text: str) -> tuple[int, int, int] | None:
    """The one parser for ``vibe --version`` output; ``None`` when unrecognised."""
    match = _VERSION.fullmatch(text.strip())
    if match is None:
        return None
    return (int(match[1]), int(match[2]), int(match[3]))


@lru_cache(maxsize=8)
def _probe(target: str, stamp: float | None) -> tuple[int, int, int] | None:
    # Keyed on the resolved path and its mtime, so a CLI upgrade re-probes once.
    try:
        result = subprocess.run(
            [target, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return parse_vibe_version(result.stdout)


def installed_vibe_version(binary: str | None = None) -> tuple[int, int, int] | None:
    """The installed CLI's version, or ``None`` when it cannot be determined.

    Callers treat ``None`` as "stay on today's argv"; never guess from a failure.
    """
    name = binary or "vibe"
    path = shutil.which(name)
    target = path or name
    try:
        stamp = Path(target).stat().st_mtime if path else None
    except OSError:
        stamp = None
    return _probe(target, stamp)


__all__ = ["installed_vibe_version", "parse_vibe_version"]
