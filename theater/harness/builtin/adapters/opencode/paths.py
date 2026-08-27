"""OpenCode structured tool-path projection."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from theater.harness.base import EventPath

from .constants import _WRITE_TOOLS


def _relativise(path: str, cwd: str | None) -> str | None:
    """Return a session-relative path, resolving absolute paths safely."""
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute():
        return path
    if cwd is None:
        # Do not index absolute paths without a session root.
        return None
    try:
        rel = p.resolve().relative_to(Path(cwd).resolve())
    except (ValueError, OSError):
        return None
    return str(rel)


def _paths_from_tool(name: str, state: dict, cwd: str | None) -> tuple[EventPath, ...]:
    """Project only structured file paths, never command or prose content."""
    if not name or name in ("bash", "shell", "apply_patch", "glob", "grep", "webfetch"):
        return ()
    input_data = state.get("input")
    if not isinstance(input_data, dict):
        return ()
    raw = input_data.get("filePath")
    if not isinstance(raw, str):
        return ()
    rel = _relativise(raw, cwd)
    if rel is None:
        return ()
    mode: Literal["read", "write"] = "write" if name in _WRITE_TOOLS else "read"
    return (EventPath(path=rel, mode=mode),)
