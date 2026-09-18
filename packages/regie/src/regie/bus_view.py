"""Forward-compatible diagnostic bus formatting for the optional panel."""

from __future__ import annotations

from collections.abc import Mapping

from regie.formatting import diagnostic_line


def format_bus_line(row: Mapping[str, object]) -> str:
    return diagnostic_line(row)


def kind_style(row: Mapping[str, object]) -> str:
    """Use a neutral style for unknown diagnostic kinds rather than rejecting them."""
    kind = row.get("kind")
    return "bold" if isinstance(kind, str) and kind.endswith(".failed") else ""


__all__ = ["format_bus_line", "kind_style"]
