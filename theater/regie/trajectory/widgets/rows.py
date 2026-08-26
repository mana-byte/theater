"""Fast row-height updates for trajectory data tables."""

from __future__ import annotations

from collections.abc import Mapping

from textual.geometry import Size
from textual.widgets import DataTable
from textual.widgets._data_table import RowKey


def resize_rows(table: DataTable, heights: Mapping[str, int]) -> bool:
    """Resize existing rows without rebuilding their table."""
    changed = False
    height_delta = 0
    for key, height in heights.items():
        if height < 1:
            raise ValueError("data table rows must be at least one line high")
        row = table.rows.get(RowKey(key))
        if row is None or row.height == height:
            continue
        height_delta += height - row.height
        row.height = height
        changed = True
    if not changed:
        return False
    table._update_count += 1
    table._clear_caches()
    if height_delta:
        table.virtual_size = Size(
            table.virtual_size.width,
            max(0, table.virtual_size.height + height_delta),
        )
    table.refresh()
    return True


__all__ = ["resize_rows"]
