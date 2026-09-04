"""Structural validation for immutable MCP-server manifests."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Mapping, Sequence
from math import isfinite
from pathlib import Path
from typing import NoReturn

from theater.constants.core import HARNESS_NAME
from theater.constants.plugins import MCP_PLUGIN_CONFIG_MAX_FIELDS, MCP_PLUGIN_DESCRIPTION_MAX_CHARS
from theater.mcp_plugins.contracts import (
    MANIFEST_API_VERSION,
    MISSING,
    McpConfigField,
    McpConfigKind,
    McpConfigSchema,
    McpLaunchManifest,
    McpServerManifest,
    PluginCapability,
)

_FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


class McpManifestValidationError(ValueError):
    """A path-qualified MCP-server manifest validation failure."""

    def __init__(self, name: str, path: str, message: str) -> None:
        self.name = name
        self.path = path
        self.message = message
        super().__init__(f"manifest {name!r}.{path}: {message}")


def validate_manifest(name: str, manifest: McpServerManifest) -> None:
    """Raise unless one MCP-server manifest is static, safe, and complete."""
    _validate_name(name)
    if not isinstance(manifest, McpServerManifest):
        _fail(name, "root", f"expected McpServerManifest, got {type(manifest).__name__}")
    if type(manifest.api_version) is not int:
        _fail(name, "api_version", "must be an integer")
    if manifest.api_version != MANIFEST_API_VERSION:
        _fail(
            name,
            "api_version",
            f"unsupported API version {manifest.api_version!r}; supported version is "
            f"{MANIFEST_API_VERSION}",
        )
    _validate_description(name, manifest.description)
    _validate_capabilities(name, manifest)
    _validate_config(name, manifest.config)
    _validate_launch(name, manifest.launch)


def _validate_name(name: object) -> None:
    display = name if isinstance(name, str) else "<invalid>"
    if not isinstance(name, str) or HARNESS_NAME.fullmatch(name) is None:
        _fail(
            display,
            "name",
            "must use lowercase letters, digits, '_' or '-', starting with a letter or digit",
        )


def _validate_description(name: str, description: object) -> None:
    if not isinstance(description, str) or not description.strip():
        _fail(name, "description", "must be a non-blank string")
    if not description.isprintable():
        _fail(name, "description", "must contain only printable characters")
    if len(description) > MCP_PLUGIN_DESCRIPTION_MAX_CHARS:
        _fail(
            name,
            "description",
            f"must contain at most {MCP_PLUGIN_DESCRIPTION_MAX_CHARS} characters",
        )


def _validate_capabilities(name: str, manifest: McpServerManifest) -> None:
    if not manifest._capabilities_were_frozen or not isinstance(manifest.capabilities, frozenset):
        _fail(name, "capabilities", "must be a non-empty frozenset of PluginCapability values")
    if not manifest.capabilities:
        _fail(name, "capabilities", "must declare at least one capability")
    for capability in manifest.capabilities:
        if not isinstance(capability, PluginCapability):
            _fail(name, "capabilities", "must contain only PluginCapability values")


def _validate_config(name: str, schema: object) -> None:
    if not isinstance(schema, McpConfigSchema):
        _fail(name, "config", f"expected McpConfigSchema, got {type(schema).__name__}")
    if not isinstance(schema.fields, Mapping):
        _fail(name, "config.fields", "must be a mapping of field names to McpConfigField values")
    if len(schema.fields) > MCP_PLUGIN_CONFIG_MAX_FIELDS:
        _fail(name, "config.fields", f"may contain at most {MCP_PLUGIN_CONFIG_MAX_FIELDS} fields")
    for field_name, spec in schema.fields.items():
        path = f"config.{field_name}" if isinstance(field_name, str) else "config.<invalid>"
        if not isinstance(field_name, str) or _FIELD_NAME.fullmatch(field_name) is None:
            _fail(name, path, "field names must be flat ASCII identifiers")
        if not isinstance(spec, McpConfigField):
            _fail(name, path, f"expected McpConfigField, got {type(spec).__name__}")
        _validate_field(name, path, spec)


def _validate_field(name: str, path: str, spec: McpConfigField) -> None:
    if not isinstance(spec.kind, McpConfigKind):
        _fail(name, f"{path}.kind", "must be an McpConfigKind")
    if type(spec.required) is not bool:
        _fail(name, f"{path}.required", "must be a boolean")
    if spec.required and spec.default is not MISSING:
        _fail(name, f"{path}.default", "must be omitted when required is true")
    _validate_range(name, path, spec)
    _validate_limits(name, path, spec)
    _validate_choices(name, path, spec)
    if type(spec.path_must_exist) is not bool:
        _fail(name, f"{path}.path_must_exist", "must be a boolean")
    if type(spec.path_must_be_absolute) is not bool:
        _fail(name, f"{path}.path_must_be_absolute", "must be a boolean")
    if spec.kind is not McpConfigKind.PATH and (spec.path_must_exist or spec.path_must_be_absolute):
        _fail(name, path, "path constraints require kind=PATH")
    if spec.default is not MISSING:
        _validate_default(name, f"{path}.default", spec, spec.default)


def _validate_range(name: str, path: str, spec: McpConfigField) -> None:
    minimum = spec.minimum
    maximum = spec.maximum
    if minimum is not None or maximum is not None:
        if spec.kind not in {McpConfigKind.INTEGER, McpConfigKind.FLOAT}:
            _fail(name, path, "numeric bounds require kind=INTEGER or FLOAT")
        for suffix, value in (("minimum", minimum), ("maximum", maximum)):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
            ):
                _fail(name, f"{path}.{suffix}", "must be a finite number or null")
        if minimum is not None and maximum is not None and minimum > maximum:
            _fail(name, path, "minimum must not exceed maximum")


def _validate_limits(name: str, path: str, spec: McpConfigField) -> None:
    limits = (
        ("min_length", spec.min_length),
        ("max_length", spec.max_length),
        ("min_items", spec.min_items),
        ("max_items", spec.max_items),
    )
    for suffix, value in limits:
        if value is not None and (type(value) is not int or value < 0):
            _fail(name, f"{path}.{suffix}", "must be a non-negative integer or null")
    if spec.min_length is not None or spec.max_length is not None:
        if spec.kind is not McpConfigKind.STRING:
            _fail(name, path, "length bounds require kind=STRING")
        if (
            spec.min_length is not None
            and spec.max_length is not None
            and spec.min_length > spec.max_length
        ):
            _fail(name, path, "min_length must not exceed max_length")
    if spec.min_items is not None or spec.max_items is not None:
        if spec.kind is not McpConfigKind.STRING_LIST:
            _fail(name, path, "item bounds require kind=STRING_LIST")
        if (
            spec.min_items is not None
            and spec.max_items is not None
            and spec.min_items > spec.max_items
        ):
            _fail(name, path, "min_items must not exceed max_items")


def _validate_choices(name: str, path: str, spec: McpConfigField) -> None:
    if spec.choices is None:
        return
    if spec.kind is not McpConfigKind.STRING:
        _fail(name, path, "choices require kind=STRING")
    if not isinstance(spec.choices, frozenset) or not spec.choices:
        _fail(name, f"{path}.choices", "must be a non-empty frozenset of strings")
    if any(not isinstance(value, str) or not value for value in spec.choices):
        _fail(name, f"{path}.choices", "must contain non-empty strings")


def _validate_default(name: str, path: str, spec: McpConfigField, value: object) -> None:
    kind = spec.kind
    if not isinstance(kind, McpConfigKind):
        _fail(name, f"{path}.kind", "must be an McpConfigKind")
    validator = _DEFAULT_VALIDATORS.get(kind)
    if validator is None:
        _fail(name, f"{path}.kind", "must be an McpConfigKind")
    validator(name, path, spec, value)


def _validate_string_default(name: str, path: str, spec: McpConfigField, value: object) -> None:
    if not isinstance(value, str):
        _fail(name, path, "must be a string")
    _check_string_constraints(name, path, spec, value)


def _validate_integer_default(name: str, path: str, spec: McpConfigField, value: object) -> None:
    if type(value) is not int:
        _fail(name, path, "must be an integer")
    _check_numeric_constraints(name, path, spec, value)


def _validate_float_default(name: str, path: str, spec: McpConfigField, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        _fail(name, path, "must be a finite number")
    _check_numeric_constraints(name, path, spec, float(value))


def _validate_boolean_default(
    name: str,
    path: str,
    _spec: McpConfigField,
    value: object,
) -> None:
    if type(value) is not bool:
        _fail(name, path, "must be a boolean")


def _validate_path_default(name: str, path: str, spec: McpConfigField, value: object) -> None:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        _fail(name, path, "must be a non-blank path")
    if spec.path_must_be_absolute and not Path(value).is_absolute():
        _fail(name, path, "must be an absolute path")


def _validate_list_default(name: str, path: str, spec: McpConfigField, value: object) -> None:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        _fail(name, path, "must be a sequence of strings")
    if any(not isinstance(item, str) for item in value):
        _fail(name, path, "must contain only strings")
    _check_item_constraints(name, path, spec, len(value))


def _validate_secret_default(
    name: str,
    path: str,
    _spec: McpConfigField,
    _value: object,
) -> None:
    _fail(name, path, "secret fields may not declare defaults")


def _check_string_constraints(name: str, path: str, spec: McpConfigField, value: str) -> None:
    if spec.min_length is not None and len(value) < spec.min_length:
        _fail(name, path, f"must be at least {spec.min_length} characters")
    if spec.max_length is not None and len(value) > spec.max_length:
        _fail(name, path, f"must be at most {spec.max_length} characters")
    if spec.choices is not None and value not in spec.choices:
        _fail(name, path, "must be one of the declared choices")


def _check_numeric_constraints(
    name: str,
    path: str,
    spec: McpConfigField,
    value: int | float,
) -> None:
    if spec.minimum is not None and value < spec.minimum:
        _fail(name, path, f"must be >= {spec.minimum}")
    if spec.maximum is not None and value > spec.maximum:
        _fail(name, path, f"must be <= {spec.maximum}")


def _check_item_constraints(name: str, path: str, spec: McpConfigField, count: int) -> None:
    if spec.min_items is not None and count < spec.min_items:
        _fail(name, path, f"must contain at least {spec.min_items} items")
    if spec.max_items is not None and count > spec.max_items:
        _fail(name, path, f"must contain at most {spec.max_items} items")


def _validate_launch(name: str, launch: object) -> None:
    if not isinstance(launch, McpLaunchManifest):
        _fail(name, "launch", f"expected McpLaunchManifest, got {type(launch).__name__}")
    if not callable(launch.planner):
        _fail(name, "launch.planner", "must be callable")
    if inspect.iscoroutinefunction(launch.planner):
        _fail(name, "launch.planner", "must be synchronous")


type _DefaultValidator = Callable[[str, str, McpConfigField, object], None]

_DEFAULT_VALIDATORS: Mapping[McpConfigKind, _DefaultValidator] = {
    McpConfigKind.STRING: _validate_string_default,
    McpConfigKind.INTEGER: _validate_integer_default,
    McpConfigKind.FLOAT: _validate_float_default,
    McpConfigKind.BOOLEAN: _validate_boolean_default,
    McpConfigKind.PATH: _validate_path_default,
    McpConfigKind.STRING_LIST: _validate_list_default,
    McpConfigKind.SECRET: _validate_secret_default,
}


def _fail(name: str, path: str, message: str) -> NoReturn:
    raise McpManifestValidationError(name, path, message)


__all__ = ["McpManifestValidationError", "validate_manifest"]
