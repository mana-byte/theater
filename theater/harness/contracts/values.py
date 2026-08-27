"""Immutable JSON-shaped contract values."""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from types import MappingProxyType


def freeze_json_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    """Deep-freeze one JSON object into immutable values."""
    return _freeze_mapping(value)


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    copied: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError("JSON object keys must be strings")
        copied[key] = _freeze_json_value(item)
    return MappingProxyType(copied)


def _freeze_json_value(value: object) -> object:
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("JSON strings must be valid UTF-8") from exc
        return value
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item) for item in value)
    raise TypeError("value must be JSON-compatible")


__all__ = ["freeze_json_mapping"]
