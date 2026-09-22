"""Standalone parsing for Régie's presentation-only configuration."""

from __future__ import annotations

import math
import tomllib
from dataclasses import fields
from pathlib import Path
from typing import Any

from regie.contracts import RegieSettings

_MIN_INTERVAL = 0.01
_TRAJECTORY_PAGE_MAX = 2_000
_KNOWN = frozenset(field.name for field in fields(RegieSettings) if field.name != "favourite")
_FLOATS = frozenset(
    {
        "tree_interval",
        "bus_interval",
        "dashboard_sentence_hold_seconds",
        "dashboard_sentence_char_interval",
        "dashboard_tip_hold_seconds",
        "dashboard_tip_char_interval",
    }
)
_POSITIVE_INTS = frozenset({"bus_batch", "cwd_segments", "trajectory_page_size"})
_BOOLEANS = frozenset({"bus_visible", "startup_reveal"})


class SettingsError(ValueError):
    """The standalone Régie configuration cannot be honoured."""


def load_settings(path: Path) -> RegieSettings:
    """Load Régie settings plus Theater's shared favourite-harness choice."""
    document = _read_document(path)
    table = document.get("regie", {})
    if not isinstance(table, dict):
        raise SettingsError(f"{path}: [regie] must be a table")
    unknown = sorted(set(table).difference(_KNOWN))
    if unknown:
        raise SettingsError(f"{path}: unknown [regie] setting {unknown[0]!r}")
    values: dict[str, Any] = {name: _validate(path, name, value) for name, value in table.items()}
    theater_document = document
    main_path = path.parent.parent / "config.toml" if path.parent.name == "regie" else path
    if main_path != path:
        theater_document = _read_document(main_path)
    theater = theater_document.get("theater", {})
    if not isinstance(theater, dict):
        raise SettingsError(f"{main_path}: [theater] must be a table")
    favourite = theater.get("favourite")
    if favourite is not None and (not isinstance(favourite, str) or not favourite):
        raise SettingsError(f"{main_path}: theater.favourite must be a non-empty string")
    values["favourite"] = favourite
    return RegieSettings(**values)


def _read_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as config_file:
            return tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SettingsError(f"{path}: could not read Régie settings: {exc}") from exc


def _validate(path: Path, name: str, value: Any) -> object:  # noqa: PLR0912
    dotted = f"regie.{name}"
    if name in _FLOATS:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise SettingsError(f"{path}: {dotted} must be a finite number")
        parsed = float(value)
        if not math.isfinite(parsed) or parsed < _MIN_INTERVAL:
            raise SettingsError(f"{path}: {dotted} must be finite and >= {_MIN_INTERVAL}")
        return parsed
    if name in _POSITIVE_INTS:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise SettingsError(f"{path}: {dotted} must be an integer >= 1")
        if name == "trajectory_page_size" and value > _TRAJECTORY_PAGE_MAX:
            raise SettingsError(f"{path}: {dotted} must be <= {_TRAJECTORY_PAGE_MAX}")
        return value
    if name == "sidebar_width":
        if isinstance(value, bool) or not isinstance(value, int) or value < 40:
            raise SettingsError(f"{path}: {dotted} must be an integer >= 40")
        return value
    if name in _BOOLEANS:
        if type(value) is not bool:
            raise SettingsError(f"{path}: {dotted} must be true or false")
        return value
    if name == "participant_detail":
        if not isinstance(value, str) or value not in {"cwd", "description"}:
            raise SettingsError(f"{path}: {dotted} must be 'cwd' or 'description'")
        return value
    if name == "dashboard_sentences":
        valid_sentences = isinstance(value, list) and all(
            isinstance(item, str) and item.strip() for item in value
        )
        if not valid_sentences:
            raise SettingsError(f"{path}: {dotted} must be a list of non-blank strings")
        return list(value)
    if name == "theme":
        if not isinstance(value, str):
            raise SettingsError(f"{path}: {dotted} must be a string")
        return value
    if name == "cost_window":
        if not isinstance(value, str):
            raise SettingsError(f"{path}: {dotted} must be a string")
        return value
    raise SettingsError(f"{path}: unsupported [regie] setting {name!r}")


__all__ = ["SettingsError", "load_settings"]
