"""Compact participant-leaf rendering from immutable public projection rows."""

from __future__ import annotations

from regie.animations import spinner_frame
from regie.render import bounded_text
from regie.tree import TreeRow


def render_leaf(
    row: TreeRow,
    *,
    selected: bool,
    staged: bool,
    trajectory: bool,
    stage_reason: str | None,
) -> str:
    """Render one stable-ID row; labels remain display-only."""
    marker = "▶" if selected else " "
    surface = "●" if staged else "◌" if trajectory else " "
    status = (
        f"{spinner_frame(0)} {row.status}" if row.status in {"working", "running"} else row.status
    )
    route = "route" if row.addressable else "no route"
    suffix = f" · {bounded_text(row.detail, limit=96)}" if row.detail else ""
    if stage_reason:
        suffix += f" · {bounded_text(stage_reason, limit=96)}"
    return f"{marker}{surface} {'  ' * row.depth}{row.label} [{status}; {route}]{suffix}"


__all__ = ["render_leaf"]
