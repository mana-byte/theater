"""Trajectory parameter validation at RPC and service boundaries."""

from __future__ import annotations

import math

from theater.constants.trajectory import (
    TRAJECTORY_CURSOR_MAX_BYTES,
    TRAJECTORY_FOLLOW_TIMEOUT_SECONDS,
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
    TRAJECTORY_PAGE_RECORD_LIMIT,
)
from theater.models import BadRequest


def rpc_required_string(params: dict, key: str, method_name: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise BadRequest(f"{method_name} requires non-empty string parameter {key!r}")
    _encoded_length(value, f"{method_name} parameter {key!r}")
    return value


def rpc_required_bounded_string(params: dict, key: str, method_name: str, maximum: int) -> str:
    value = rpc_required_string(params, key, method_name)
    _validate_encoded_length(value, maximum, f"{method_name} parameter {key!r}")
    return value


def rpc_required_identifier(params: dict, key: str, method_name: str) -> str:
    value = rpc_required_bounded_string(params, key, method_name, TRAJECTORY_IDENTIFIER_MAX_BYTES)
    if _has_control_characters(value):
        raise BadRequest(f"{method_name} parameter {key!r} must not contain control characters")
    return value


def rpc_optional_cursor(params: dict, key: str, method_name: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise BadRequest(f"{method_name} parameter {key!r} must be a non-empty string or null")
    if _encoded_length(value, f"{method_name} parameter {key!r}") > TRAJECTORY_CURSOR_MAX_BYTES:
        raise BadRequest(f"{method_name} parameter {key!r} exceeds the cursor limit")
    return value


def rpc_optional_stream_id(params: dict, method_name: str) -> str | None:
    value = params.get("stream_id")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise BadRequest(f"{method_name} parameter 'stream_id' must be a non-empty string or null")
    if (
        _encoded_length(value, f"{method_name} parameter 'stream_id'")
        > TRAJECTORY_IDENTIFIER_MAX_BYTES
    ):
        raise BadRequest(f"{method_name} parameter 'stream_id' exceeds the identifier limit")
    return value


def validate_participant_token(value: object, method_name: str) -> None:
    _validate_required_bounded_string(
        value,
        TRAJECTORY_IDENTIFIER_MAX_BYTES,
        f"{method_name} requires non-empty string parameter 'id'",
        f"{method_name} parameter 'id'",
    )


def validate_identifier(value: object, method_name: str, key: str) -> None:
    string = _validate_required_bounded_string(
        value,
        TRAJECTORY_IDENTIFIER_MAX_BYTES,
        f"{method_name} requires non-empty string parameter {key!r}",
        f"{method_name} parameter {key!r}",
    )
    if _has_control_characters(string):
        raise BadRequest(f"{method_name} parameter {key!r} must not contain control characters")


def validate_bounded_string(value: object, key: str, maximum: int) -> None:
    _validate_required_bounded_string(
        value,
        maximum,
        f"trajectory parameter {key!r} must be a non-empty string",
        f"trajectory parameter {key!r}",
    )


def validate_optional_cursor(value: object, method_name: str) -> None:
    if value is None:
        return
    _validate_required_bounded_string(
        value,
        TRAJECTORY_CURSOR_MAX_BYTES,
        f"{method_name} parameter 'before' must be a non-empty string or null",
        f"{method_name} parameter 'before'",
    )


def validate_limit(value: object, method_name: str) -> int:
    if type(value) is not int or value <= 0:
        raise BadRequest(f"{method_name} parameter 'limit' must be a positive integer")
    return min(value, TRAJECTORY_PAGE_RECORD_LIMIT)


def validate_rpc_wait(value: object, method_name: str) -> float:
    return _validate_wait(value, method_name, exact_numeric_type=True)


def validate_wait(value: object, method_name: str) -> float:
    return _validate_wait(value, method_name, exact_numeric_type=False)


def _validate_required_bounded_string(
    value: object, maximum: int, required_message: str, label: str
) -> str:
    if not isinstance(value, str) or not value:
        raise BadRequest(required_message)
    _validate_encoded_length(value, maximum, label)
    return value


def _encoded_length(value: str, label: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise BadRequest(f"{label} must contain valid UTF-8") from exc


def _validate_encoded_length(value: str, maximum: int, label: str) -> None:
    if _encoded_length(value, label) > maximum:
        raise BadRequest(f"{label} exceeds {maximum} encoded bytes")


def _validate_wait(value: object, method_name: str, *, exact_numeric_type: bool) -> float:
    if isinstance(value, bool):
        raise BadRequest(f"{method_name} parameter 'wait' must be a non-negative finite number")
    if exact_numeric_type:
        if type(value) not in (int, float):
            raise BadRequest(f"{method_name} parameter 'wait' must be a non-negative finite number")
    elif not isinstance(value, (int, float)):
        raise BadRequest(f"{method_name} parameter 'wait' must be a non-negative finite number")
    assert isinstance(value, (int, float))
    if not math.isfinite(value) or value < 0:
        raise BadRequest(f"{method_name} parameter 'wait' must be a non-negative finite number")
    return min(float(value), TRAJECTORY_FOLLOW_TIMEOUT_SECONDS)


def _has_control_characters(value: str) -> bool:
    return any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value)
