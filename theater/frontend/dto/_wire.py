"""Small strict readers for immutable public wire values."""

from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType

type JSONScalar = bool | int | float | str | None
type JSONValue = JSONScalar | tuple[JSONValue, ...] | Mapping[str, JSONValue]


def freeze_json(value: object, label: str = "value") -> JSONValue:
    """Validate and recursively freeze one JSON value."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} must not contain non-finite numbers")
        return value
    if isinstance(value, Mapping):
        result: dict[str, JSONValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{label} object keys must be strings")
            result[key] = freeze_json(item, f"{label}.{key}")
        return MappingProxyType(result)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item, f"{label}[]") for item in value)
    raise TypeError(f"{label} must be JSON-compatible")


def object_value(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise TypeError(f"{label} object keys must be strings")
    return value


def string_value(value: object, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise TypeError(f"{label} must be a non-empty string")
    return value


def integer_value(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise TypeError(f"{label} must be an integer greater than or equal to {minimum}")
    return value


def boolean_value(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{label} must be a boolean")
    return value


def extras(value: Mapping[str, object], known: set[str]) -> Mapping[str, JSONValue]:
    return MappingProxyType(
        {key: freeze_json(item, key) for key, item in value.items() if key not in known}
    )


def thaw_json(value: JSONValue) -> object:
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def append_extras(result: dict[str, object], extra: Mapping[str, JSONValue]) -> dict[str, object]:
    for key, value in extra.items():
        if key not in result:
            result[key] = thaw_json(value)
    return result
