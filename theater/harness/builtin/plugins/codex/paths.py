"""Codex transcript path extraction and normalization."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from theater.harness.base import EventPath

from .constants import _PATCH_FILE_RE


def _event_path(value: str, *, cwd: str | None, mode: Literal["read", "write"]) -> EventPath | None:
    path = Path(value)
    if path.is_absolute():
        if cwd is None:
            return None
        try:
            path = path.resolve(strict=False).relative_to(Path(cwd).resolve(strict=False))
        except (OSError, ValueError):
            return None
    elif ".." in path.parts:
        return None
    rendered = path.as_posix()
    if rendered in {"", "."} or len(rendered) > 2048:
        return None
    return EventPath(path=rendered, mode=mode)


def _apply_patch_paths(text: str, *, cwd: str | None = None) -> tuple[EventPath, ...]:
    """Extract write paths from structured apply-patch markers."""
    if not isinstance(text, str):
        return ()
    paths = (
        _event_path(match.strip(), cwd=cwd, mode="write") for match in _PATCH_FILE_RE.findall(text)
    )
    return tuple(path for path in paths if path is not None)


def _patch_change_paths(value: object, *, cwd: str | None) -> tuple[EventPath, ...]:
    if not isinstance(value, Mapping):
        return ()
    paths: list[EventPath] = []
    for raw_path, change in value.items():
        if not isinstance(raw_path, str) or not isinstance(change, Mapping):
            continue
        if change.get("type") not in {"add", "delete", "update"}:
            continue
        candidates = [raw_path]
        move_path = change.get("move_path")
        if isinstance(move_path, str):
            candidates.append(move_path)
        paths.extend(
            path
            for candidate in candidates
            if (path := _event_path(candidate, cwd=cwd, mode="write")) is not None
        )
    return tuple(dict.fromkeys(paths))
