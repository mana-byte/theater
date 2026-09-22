"""Strict public-frame parsing and schema-validated response encoding."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NoReturn

from jsonschema.exceptions import ValidationError

from theater import protocol
from theater.frontend.capabilities import MAX_EXACT_JSON_INTEGER, MAX_FRAME_BYTES
from theater.frontend.schemas import validate_public_request, validate_public_response


@dataclass(frozen=True, slots=True)
class PublicRequestError(Exception):
    """A bounded structured refusal suitable for a public response."""

    code: str
    message: str
    details: Mapping[str, object] | None = None

    def __str__(self) -> str:
        return self.message


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number {value!r} is not allowed")


def _reject_surrogates(value: object) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("JSON strings must contain valid Unicode scalar values")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_surrogates(key)
            _reject_surrogates(item)
    elif isinstance(value, list):
        for item in value:
            _reject_surrogates(item)


def parse_public_frame(line: bytes) -> dict[str, Any]:
    """Decode one strict JSON object without accepting non-finite or surrogate values."""
    try:
        value = json.loads(line, parse_constant=_reject_constant)
        _reject_surrogates(value)
    except (UnicodeError, ValueError) as exc:
        raise PublicRequestError("bad_request", f"malformed public JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise PublicRequestError("bad_request", "public request must be a JSON object")
    return value


def correlated_id(value: object) -> int:
    """Return a usable positive public request ID, or zero when uncorrelated."""
    if type(value) is int and 0 < value <= MAX_EXACT_JSON_INTEGER:
        return value
    return 0


def validate_request(value: Mapping[str, Any]) -> None:
    try:
        validate_public_request(value)
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise PublicRequestError("bad_request", f"invalid public request: {exc}") from exc


def success_response(method: str, request_id: int, result: object) -> bytes:
    value = {"id": request_id, "ok": True, "result": result}
    validate_public_response(method, value)
    encoded = protocol.encode(value)
    if len(encoded) <= MAX_FRAME_BYTES:
        return encoded
    return error_response(
        request_id,
        "too_large",
        f"the successful response exceeds the {MAX_FRAME_BYTES}-byte public frame limit",
    )


def error_response(
    request_id: int,
    code: str,
    message: str,
    *,
    details: Mapping[str, object] | None = None,
) -> bytes:
    """Encode one bounded error without recursively producing another error."""
    request_id = correlated_id(request_id)
    bounded_message = message[:8192]
    bounded_code = code[:512] or "internal"
    error: dict[str, object] = {"code": bounded_code, "message": bounded_message}
    if details is not None:
        error["details"] = dict(details)
    value: dict[str, object] = {
        "id": request_id,
        "ok": False,
        "error": error,
    }
    try:
        validate_public_response("frontend.handshake", value)
    except Exception:
        error.pop("details", None)
        validate_public_response("frontend.handshake", value)
    encoded = protocol.encode(value)
    if len(encoded) <= MAX_FRAME_BYTES:
        return encoded

    error.pop("details", None)
    error["message"] = "request failed; error details exceeded the public frame limit"
    encoded = protocol.encode(value)
    if len(encoded) <= MAX_FRAME_BYTES:
        return encoded

    # An injected ceiling smaller than the minimum envelope cannot carry JSON.
    value = {
        "id": request_id,
        "ok": False,
        "error": {"code": "too_large", "message": "response exceeds public frame limit"},
    }
    encoded = protocol.encode(value)
    return encoded if len(encoded) <= MAX_FRAME_BYTES else b""


__all__ = [
    "PublicRequestError",
    "correlated_id",
    "error_response",
    "parse_public_frame",
    "success_response",
    "validate_request",
]
