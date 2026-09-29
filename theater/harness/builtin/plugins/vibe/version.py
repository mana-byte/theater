"""Installed Vibe version detection, shared by the probe and the launch planner."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

_VERSION = re.compile(r"vibe (\d+)\.(\d+)\.(\d+)")
_CACHE_LIMIT = 8


def parse_vibe_version(text: str) -> tuple[int, int, int] | None:
    """The one parser for ``vibe --version`` output; ``None`` when unrecognised."""
    match = _VERSION.fullmatch(text.strip())
    if match is None:
        return None
    return (int(match[1]), int(match[2]), int(match[3]))


# Keyed on the resolved path and its mtime, so a CLI upgrade re-probes once.
# Only successes are kept: a transient timeout must not pin the guess for the daemon's life.
_CACHE: dict[tuple[str, float | None], tuple[int, int, int]] = {}


def _probe(target: str) -> tuple[int, int, int] | None:
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
    key = (target, stamp)
    if key not in _CACHE:
        found = _probe(target)
        if found is None:
            return None
        if len(_CACHE) >= _CACHE_LIMIT:
            _CACHE.clear()
        _CACHE[key] = found
    return _CACHE[key]


__all__ = ["installed_vibe_version", "parse_vibe_version"]
