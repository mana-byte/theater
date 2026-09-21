"""Load and validate the bundled RC10 schema catalog without network access."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from functools import lru_cache
from importlib import resources
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

from theater.frontend.capabilities import CALLBACK_CATALOG, METHOD_CATALOG

SCHEMA_BASE_URI = "https://theater.dev/schemas/frontend/1.0/"
RESOURCE_NAMES = (
    "common.json",
    "envelopes.json",
    "methods.json",
    "callbacks.json",
    "events.json",
    "errors.json",
)

# Large immutable observation payloads are validated outside interactive event loops.
BULK_RESPONSE_METHODS = frozenset(
    {
        "frontend.trajectory.snapshot",
        "frontend.trajectory.follow",
        "frontend.trajectory.search",
        "frontend.transcripts.read",
        "frontend.recall.read",
    }
)


@lru_cache(maxsize=1)
def load_schema_resources() -> Mapping[str, Mapping[str, Any]]:
    """Return immutable URI-to-document mappings for bundled resources."""
    loaded: dict[str, Mapping[str, Any]] = {}
    root = resources.files("theater.frontend.schemas")
    for name in RESOURCE_NAMES:
        value = json.loads(root.joinpath(name).read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("$id") != f"{SCHEMA_BASE_URI}{name}":
            raise ValueError(f"schema resource {name} has an invalid or missing $id")
        Draft202012Validator.check_schema(value)
        loaded[value["$id"]] = MappingProxyType(value)
    return MappingProxyType(loaded)


@lru_cache(maxsize=1)
def schema_registry() -> Registry:
    """Build an offline registry containing every normative resource."""
    registry = Registry()
    for uri, schema in load_schema_resources().items():
        registry = registry.with_resource(uri, Resource.from_contents(schema))
    return registry


def validator_for(schema_id: str) -> Draft202012Validator:
    """Resolve a stable schema ID solely through the bundled registry."""
    if not isinstance(schema_id, str) or not schema_id.startswith(SCHEMA_BASE_URI):
        raise KeyError(f"schema is not in the bundled RC10 catalog: {schema_id!r}")
    validator = Draft202012Validator({"$ref": schema_id}, registry=schema_registry())
    validator.check_schema(validator.schema)
    return validator


def _reject_nonfinite(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValidationError("public frames must not contain non-finite numbers")
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_nonfinite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_nonfinite(item)


def validate_public_request(value: object) -> None:
    """Validate one complete request and its method-specific parameters."""
    _reject_nonfinite(value)
    if not isinstance(value, Mapping):
        raise TypeError("public request must be an object")
    method = value.get("method")
    if method == "frontend.handshake":
        spec = METHOD_CATALOG[method]
        validator_for(spec.request_schema_id).validate(value)
        validator_for(spec.params_schema_id).validate(value.get("params"))
        return
    if not isinstance(method, str) or method not in METHOD_CATALOG:
        raise KeyError(f"unknown public method: {method!r}")
    spec = METHOD_CATALOG[method]
    validator_for(spec.request_schema_id).validate(value)
    validator_for(spec.params_schema_id).validate(value.get("params"))
    has_key = "idempotency_key" in value
    if spec.idempotency_required != has_key:
        requirement = "required" if spec.idempotency_required else "not allowed"
        raise ValueError(f"idempotency_key is {requirement} for {method}")


def validate_public_response(method: str, value: object) -> None:
    """Validate an envelope and its successful method result."""
    _reject_nonfinite(value)
    try:
        spec = METHOD_CATALOG[method]
    except KeyError as exc:
        raise KeyError(f"unknown public method: {method!r}") from exc
    validator_for(spec.response_schema_id).validate(value)
    if not isinstance(value, Mapping) or value.get("ok") is not True:
        return
    validator_for(spec.result_schema_id).validate(value.get("result"))


def validate_callback_request(value: object) -> None:
    """Validate a complete daemon-to-provider callback frame."""
    _reject_nonfinite(value)
    if not isinstance(value, Mapping):
        raise TypeError("provider callback must be an object")
    method = value.get("method")
    if not isinstance(method, str) or method not in CALLBACK_CATALOG:
        raise KeyError(f"unknown provider callback: {method!r}")
    spec = CALLBACK_CATALOG[method]
    validator_for(spec.request_schema_id).validate(value)
    validator_for(spec.params_schema_id).validate(value.get("params"))


def validate_callback_response(method: str, value: object) -> None:
    """Validate a provider callback response and its successful result."""
    _reject_nonfinite(value)
    try:
        spec = CALLBACK_CATALOG[method]
    except KeyError as exc:
        raise KeyError(f"unknown provider callback: {method!r}") from exc
    validator_for(spec.response_schema_id).validate(value)
    if not isinstance(value, Mapping) or "result" not in value:
        return
    validator_for(spec.result_schema_id).validate(value["result"])


__all__ = [
    "BULK_RESPONSE_METHODS",
    "RESOURCE_NAMES",
    "SCHEMA_BASE_URI",
    "load_schema_resources",
    "schema_registry",
    "validate_callback_request",
    "validate_callback_response",
    "validate_public_request",
    "validate_public_response",
    "validator_for",
]
