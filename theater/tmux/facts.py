"""Environment and version facts about tmux.

``available`` / ``inside_tmux`` / ``current_pane`` answer questions about the
host environment. The version probe caches after the first call because a
running tmux server cannot change version underneath us.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

from theater.tmux.command import TmuxError


def available() -> bool:
    return shutil.which("tmux") is not None


def inside_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


def current_pane() -> str | None:
    """The pane of the *calling* process, if it is itself inside tmux."""
    return os.environ.get("TMUX_PANE")


# ---- version probe (cached; tmux -V runs once) ---------------------------
_UNPROBED = object()
_VERSION_CACHE: list[str | object | None] = [_UNPROBED]


def reset_version_cache() -> None:
    """Clear the cached tmux version so tests can control the probe."""
    _VERSION_CACHE[0] = _UNPROBED


def tmux_version() -> str | None:
    """The raw tmux version string, e.g. ``"3.7"``, ``"3.7a"``, ``"3.4"``.

    Returns ``None`` if tmux is absent or the output is unparseable. Never
    raises. The leading ``tmux `` prefix from ``tmux -V`` is stripped.
    """
    from theater.tmux.client import RUN_TIMEOUT, available

    cached = _VERSION_CACHE[0]
    if cached is not _UNPROBED:
        return cached  # type: ignore[return-value]
    if not available():
        _VERSION_CACHE[0] = None
        return None
    try:
        proc = subprocess.run(
            ["tmux", "-V"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="backslashreplace",
            timeout=RUN_TIMEOUT,
            check=False,
        )
    except Exception:
        _VERSION_CACHE[0] = None
        return None
    out = proc.stdout.strip()
    if not out.startswith("tmux "):
        _VERSION_CACHE[0] = None
        return None
    version = out[len("tmux ") :]
    _VERSION_CACHE[0] = version
    return version


def _parse_version_tuple(version: str) -> tuple[int, ...] | None:
    """Parse numeric components: ``"3.7a"`` → ``(3, 7)``, ``"1.2.3"`` → ``(1, 2, 3)``.

    Returns ``None`` for non-numeric garbage like ``"master"``. A letter suffix
    is stripped, so ``"3.7a"`` parses as ``(3, 7)``. Strings like ``"next-3.8"``
    are handled by searching for the first numeric component.
    """
    m = re.search(r"\d+(?:\.\d+)*", version)
    if not m:
        return None
    return tuple(int(p) for p in m.group().split("."))


def tmux_at_least(major: int, minor: int = 0) -> bool:
    """True if the running tmux is at least ``major.minor``.

    ``"3.7a"`` counts as ≥ 3.7 because the letter suffix denotes a patch
    release on top of the bare version. Returns ``False`` if tmux is absent or
    the version is unparseable.
    """
    from theater.tmux.client import tmux_version

    version = tmux_version()
    if version is None:
        return False
    parsed = _parse_version_tuple(version)
    if parsed is None:
        return False
    # Pad the shorter tuple with zeros so (3,) >= (3, 0) is True.
    target: tuple[int, ...] = (major, minor)
    if len(parsed) < len(target):
        parsed = parsed + (0,) * (len(target) - len(parsed))
    elif len(target) < len(parsed):
        target = target + (0,) * (len(parsed) - len(target))
    return parsed >= target


def current_session_sync() -> str | None:
    """Session name of the calling process, or None if not inside tmux."""
    from theater.tmux.client import available, inside_tmux, run_sync

    if not inside_tmux() or not available():
        return None
    try:
        return run_sync("display-message", "-p", "#{session_name}") or None
    except TmuxError:
        return None
