"""Turn JSON Schema request failures into ``bad_request`` text an agent can act on.

A raw jsonschema error reaches the caller as ``internal`` with a schema dump; the caller
needs the field, the allowed values, and what it sent.
"""

from __future__ import annotations

from collections.abc import Iterable

from jsonschema.exceptions import ValidationError

from theater.models import BadRequest


def _field(error: ValidationError) -> str:
    return ".".join(str(part) for part in error.absolute_path) or "params"


def describe_validation_error(error: ValidationError) -> str:
    """One line per failing field: enums list the allowed values and echo the value sent."""
    field = _field(error)
    if error.validator == "enum" and isinstance(error.validator_value, list):
        allowed = "|".join(str(value) for value in error.validator_value)
        return f"{field} must be one of {allowed}, got {error.instance!r}"
    return f"{field}: {error.message}"


def bad_request_from(errors: Iterable[ValidationError]) -> BadRequest:
    """A single BadRequest listing every failing field once, in field order."""
    lines = sorted({describe_validation_error(error) for error in errors})
    return BadRequest("invalid request: " + "; ".join(lines))
