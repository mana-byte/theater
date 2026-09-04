"""Schema-driven resolution of enabled MCP-plugin configuration."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import NoReturn

from theater.mcp_plugins.contracts import (
    MISSING,
    McpConfigField,
    McpConfigKind,
    McpConfigSchema,
    SecretReference,
    SecretValue,
)

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class McpConfigResolutionError(ValueError):
    """An enabled MCP-plugin configuration could not be validated safely."""

    def __init__(self, plugin: str, field: str | None, message: str) -> None:
        self.plugin = plugin
        self.field = field
        self.message = message
        location = plugin if field is None else f"{plugin}.{field}"
        super().__init__(f"MCP plugin config {location}: {message}")


def resolve_config(
    plugin: str,
    schema: McpConfigSchema,
    raw: Mapping[str, object] | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Mapping[str, object]:
    """Resolve one enabled plugin's flat values into immutable planner input."""
    if not isinstance(schema, McpConfigSchema):
        raise McpConfigResolutionError(
            plugin,
            None,
            "manifest declares no valid configuration schema",
        )
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise McpConfigResolutionError(plugin, None, "configuration must be a table")
    for key in raw:
        if not isinstance(key, str) or key not in schema.fields:
            unknown_field = key if isinstance(key, str) else None
            raise McpConfigResolutionError(plugin, unknown_field, "is not declared by this plugin")

    resolved: dict[str, object] = {}
    secret_environment = os.environ if environ is None else environ
    for field_name, field in schema.fields.items():
        if field_name not in raw:
            if field.default is not MISSING:
                resolved[field_name] = _resolve_value(
                    plugin,
                    field_name,
                    field,
                    field.default,
                    secret_environment,
                )
            elif field.required:
                raise McpConfigResolutionError(plugin, field_name, "is required")
            continue
        resolved[field_name] = _resolve_value(
            plugin,
            field_name,
            field,
            raw[field_name],
            secret_environment,
        )
    return MappingProxyType(resolved)


def parse_secret_reference(plugin: str, field: str, value: object) -> SecretReference:
    """Parse exactly one supported redacted secret-reference inline table."""
    if not isinstance(value, Mapping):
        raise McpConfigResolutionError(
            plugin,
            field,
            'must be exactly one inline table: { env = "NAME" } or { file = "/path" }',
        )
    keys = set(value)
    if keys == {"env"}:
        target = value["env"]
        if not isinstance(target, str) or _ENV_NAME.fullmatch(target) is None:
            raise McpConfigResolutionError(
                plugin,
                field,
                "secret env reference must name an environment variable",
            )
        return SecretReference(source="env", target=target)
    if keys == {"file"}:
        target = value["file"]
        if not isinstance(target, str) or not target.strip():
            raise McpConfigResolutionError(plugin, field, "secret file reference must name a path")
        return SecretReference(source="file", target=target)
    raise McpConfigResolutionError(
        plugin,
        field,
        'must be exactly one inline table: { env = "NAME" } or { file = "/path" }',
    )


def _resolve_value(
    plugin: str,
    name: str,
    field: McpConfigField,
    value: object,
    environ: Mapping[str, str],
) -> object:
    kind = field.kind
    if not isinstance(kind, McpConfigKind):
        _fail(plugin, name, "has an unsupported configuration kind")
    resolver = _VALUE_RESOLVERS.get(kind)
    if resolver is None:
        _fail(plugin, name, "has an unsupported configuration kind")
    return resolver(plugin, name, field, value, environ)


def _resolve_string(
    plugin: str,
    name: str,
    field: McpConfigField,
    value: object,
    _environ: Mapping[str, str],
) -> str:
    if not isinstance(value, str):
        _fail(plugin, name, "must be a string")
    _check_string(plugin, name, field, value)
    return value


def _resolve_integer(
    plugin: str,
    name: str,
    field: McpConfigField,
    value: object,
    _environ: Mapping[str, str],
) -> int:
    if type(value) is not int:
        _fail(plugin, name, "must be an integer")
    _check_number(plugin, name, field, value)
    return value


def _resolve_float(
    plugin: str,
    name: str,
    field: McpConfigField,
    value: object,
    _environ: Mapping[str, str],
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        _fail(plugin, name, "must be a finite number")
    parsed = float(value)
    _check_number(plugin, name, field, parsed)
    return parsed


def _resolve_boolean(
    plugin: str,
    name: str,
    _field: McpConfigField,
    value: object,
    _environ: Mapping[str, str],
) -> bool:
    if type(value) is not bool:
        _fail(plugin, name, "must be true or false")
    return value


def _resolve_path(
    plugin: str,
    name: str,
    field: McpConfigField,
    value: object,
    _environ: Mapping[str, str],
) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        _fail(plugin, name, "must be a non-blank path")
    path = Path(value)
    if field.path_must_be_absolute and not path.is_absolute():
        _fail(plugin, name, "must be an absolute path")
    if field.path_must_exist and not path.exists():
        _fail(plugin, name, "must name an existing path")
    return path


def _resolve_string_list(
    plugin: str,
    name: str,
    field: McpConfigField,
    value: object,
    _environ: Mapping[str, str],
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        _fail(plugin, name, "must be a list of strings")
    if any(not isinstance(item, str) for item in value):
        _fail(plugin, name, "must be a list of strings")
    items = tuple(value)
    if field.min_items is not None and len(items) < field.min_items:
        _fail(plugin, name, f"must contain at least {field.min_items} items")
    if field.max_items is not None and len(items) > field.max_items:
        _fail(plugin, name, f"must contain at most {field.max_items} items")
    return items


def _resolve_secret_value(
    plugin: str,
    name: str,
    _field: McpConfigField,
    value: object,
    environ: Mapping[str, str],
) -> SecretValue:
    reference = parse_secret_reference(plugin, name, value)
    return _resolve_secret(plugin, name, reference, environ)


def _resolve_secret(
    plugin: str,
    name: str,
    reference: SecretReference,
    environ: Mapping[str, str],
) -> SecretValue:
    try:
        if reference.source == "env":
            value = environ[reference.target]
        else:
            value = Path(reference.target).read_text(encoding="utf-8")
    except (KeyError, OSError, UnicodeError):
        _fail(plugin, name, "secret reference could not be resolved")
    if not isinstance(value, str):
        _fail(plugin, name, "secret reference could not be resolved")
    return SecretValue(value)


def _check_string(plugin: str, name: str, field: McpConfigField, value: str) -> None:
    if field.min_length is not None and len(value) < field.min_length:
        _fail(plugin, name, f"must be at least {field.min_length} characters")
    if field.max_length is not None and len(value) > field.max_length:
        _fail(plugin, name, f"must be at most {field.max_length} characters")
    if field.choices is not None and value not in field.choices:
        _fail(plugin, name, "must be one of the declared choices")


def _check_number(plugin: str, name: str, field: McpConfigField, value: int | float) -> None:
    if field.minimum is not None and value < field.minimum:
        _fail(plugin, name, f"must be >= {field.minimum}")
    if field.maximum is not None and value > field.maximum:
        _fail(plugin, name, f"must be <= {field.maximum}")


type _ValueResolver = Callable[
    [str, str, McpConfigField, object, Mapping[str, str]],
    object,
]

_VALUE_RESOLVERS: Mapping[McpConfigKind, _ValueResolver] = {
    McpConfigKind.STRING: _resolve_string,
    McpConfigKind.INTEGER: _resolve_integer,
    McpConfigKind.FLOAT: _resolve_float,
    McpConfigKind.BOOLEAN: _resolve_boolean,
    McpConfigKind.PATH: _resolve_path,
    McpConfigKind.STRING_LIST: _resolve_string_list,
    McpConfigKind.SECRET: _resolve_secret_value,
}


def _fail(plugin: str, field: str, message: str) -> NoReturn:
    raise McpConfigResolutionError(plugin, field, message)


resolve_plugin_config = resolve_config

__all__ = [
    "McpConfigResolutionError",
    "parse_secret_reference",
    "resolve_config",
    "resolve_plugin_config",
]
