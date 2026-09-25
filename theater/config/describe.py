"""Resolved-configuration descriptions for ``theater config``, ordered as the file is written."""

from __future__ import annotations

from dataclasses import fields

from theater.config.models import (
    _SECTIONS,
    MCP_SECTION,
    MODELS_SECTION,
    REASONING_SECTION,
    Config,
)


def describe(config: Config) -> list[tuple[str, str, str]]:
    """Every setting as (dotted key, value, source), in the order a user would write the file."""
    rows: list[tuple[str, str, str]] = []
    for name, cls in _SECTIONS.items():
        section = getattr(config, name)
        for f in fields(cls):
            dotted = f"{name}.{f.name}"
            value = getattr(section, f.name)
            shown = "(unset)" if value is None else str(value)
            rows.append((dotted, shown, config.source(dotted)))
    rows.append((f"{MCP_SECTION}.enabled", str(config.mcp.enabled), config.source("mcp.enabled")))
    for name in sorted(config.mcp.plugins):
        rows.append((f"{MCP_SECTION}.plugins.{name}", "(configured)", "config.toml"))
    # `[models]` has no fields to enumerate; a harness absent from the file has no row.
    for harness in sorted(config.models):
        dotted = f"{MODELS_SECTION}.{harness}"
        rows.append((dotted, str(config.models[harness]), config.source(dotted)))
    for harness in sorted(config.reasoning):
        dotted = f"{REASONING_SECTION}.{harness}"
        rows.append((dotted, str(config.reasoning[harness]), config.source(dotted)))
    return rows
