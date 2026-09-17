"""Bound exception details for durable public mutation outcomes."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping


def operation_error(
    exc: Exception,
    *,
    default_code: str = "control_failed",
) -> dict[str, object]:
    """Return an error value accepted by the frozen public schema."""
    try:
        raw_code = getattr(exc, "code", default_code)
    except Exception:
        raw_code = default_code
    code = raw_code if isinstance(raw_code, str) and raw_code else default_code
    try:
        message = str(exc)
    except Exception:
        message = type(exc).__name__
    error: dict[str, object] = {"code": code[:512], "message": message[:8192]}
    try:
        details = getattr(exc, "details", None)
    except Exception:
        details = None
    if isinstance(details, Mapping):
        normalized = _json_details(details)
        if normalized is not None:
            error["details"] = normalized
    return error


def _json_details(value: Mapping[object, object]) -> dict[str, object] | None:
    """Keep bounded JSON details; invalid exception payloads are discarded."""

    def normalize(item: object, depth: int = 0) -> object:
        if depth > 16:
            raise ValueError
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError
            return item
        if isinstance(item, str):
            return item[:1_048_576]
        if isinstance(item, (list, tuple)) and len(item) <= 500:
            return [normalize(child, depth + 1) for child in item]
        if isinstance(item, Mapping) and len(item) <= 2048:
            if any(not isinstance(key, str) or len(key) > 512 for key in item):
                raise ValueError
            return {str(key): normalize(child, depth + 1) for key, child in item.items()}
        raise ValueError

    try:
        if len(value) > 2048 or any(not isinstance(key, str) or len(key) > 512 for key in value):
            return None
        normalized = {str(key): normalize(item) for key, item in value.items()}
        if len(json.dumps(normalized, separators=(",", ":")).encode("utf-8")) > 65_536:
            return None
    except Exception:
        return None
    return normalized


__all__ = ["operation_error"]
