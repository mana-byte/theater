"""Validation and normalization for paths recorded in the touch index."""

from __future__ import annotations

import os
from pathlib import Path


def normalize_touch_path(cwd: str | Path, raw_path: str) -> str | None:
    """Return a safe relative path, or ``None`` when it escapes ``cwd``."""
    if not isinstance(raw_path, str):
        return None

    try:
        cwd_path = Path(cwd)
        if Path(raw_path).is_absolute():
            return None

        canonical_cwd = cwd_path.resolve(strict=False)
        raw_resolved = (cwd_path / raw_path).resolve(strict=False)
        if not raw_resolved.is_relative_to(canonical_cwd):
            return None

        normalized = os.path.normpath(raw_path)
        normalized_path = Path(normalized)
        if normalized_path.is_absolute() or ".." in normalized_path.parts:
            return None

        normalized_resolved = (cwd_path / normalized).resolve(strict=False)
        if not normalized_resolved.is_relative_to(canonical_cwd):
            return None
        if normalized_resolved != raw_resolved:
            return None
    except (OSError, RuntimeError, ValueError):
        return None

    return normalized


def is_canonical_touch_path(cwd: str | Path, raw_path: str) -> bool:
    """Whether ``raw_path`` is already the safe canonical spelling."""
    return normalize_touch_path(cwd, raw_path) == raw_path
