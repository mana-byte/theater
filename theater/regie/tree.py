"""Compatibility façade re-exporting the complete previously-consumed surface.

The tree renderer is split into:

- :mod:`theater.regie.render.layout` — key types, path shortening, forest walk
- :mod:`theater.regie.render.glyphs` — spinner, working pulse, status, overlays
- :mod:`theater.regie.render.routing` — rail grid, BFS routes, send/await traces

Immutable UI values moved to :mod:`theater.constants.regie`. This module
re-exports everything the app and tests currently import so no call site
changes.
"""

from __future__ import annotations

# ruff: noqa: I001
from theater.constants.regie import (
    REGIE_SEND_TRACE_STYLE as SEND_STYLE,
    REGIE_SPINNER_FRAMES as _SPINNER_FRAMES,
    REGIE_TREE_BRANCH as _BRANCH,
    REGIE_TREE_GAP as _GAP,
    REGIE_TREE_LAST_BRANCH as _LAST_BRANCH,
    REGIE_TREE_LEAF_ROWS as LEAF_ROWS,
    REGIE_TREE_RAIL as _RAIL,
    REGIE_WORKING_HARNESS_STYLES as _WORKING_HARNESS_STYLES,
)
from theater.regie.animations.pulse import working_harness_style
from theater.regie.render.glyphs import (
    LeafCell,
    OverlayGlyph,
    _append_working_harness_parts,
    _append_working_harness_text,
    _id_style,
    _overlay_piece,
    _overlay_row,
    _rail_above,
    _status_glyph,
    node_label,
    spinner_frame,
)
from theater.regie.render.layout import (
    Key,
    _labelled,
    _walk,
    is_root_prefix,
    render_tree,
    selected_participant,
    shorten_path,
)
from theater.regie.render.routing import (
    DOWN,
    LEFT,
    RIGHT,
    UP,
    AwaitCell,
    Cell,
    Direction,
    _await_route,
    _rail_cells,
    _rail_leaves,
    _route,
    await_highlight_cells,
    await_path,
    cell_leaf,
    send_path,
    tree_glyph_at,
)

# ruff: noqa: RUF022
__all__ = [
    # Type aliases
    "Key",
    "Cell",
    "LeafCell",
    "Direction",
    "OverlayGlyph",
    # Direction constants
    "UP",
    "DOWN",
    "LEFT",
    "RIGHT",
    # Public constants
    "LEAF_ROWS",
    "SEND_STYLE",
    # Private constants (legacy aliases)
    "_SPINNER_FRAMES",
    "_WORKING_HARNESS_STYLES",
    "_BRANCH",
    "_LAST_BRANCH",
    "_RAIL",
    "_GAP",
    # Layout
    "shorten_path",
    "is_root_prefix",
    "_walk",
    "_labelled",
    "render_tree",
    "selected_participant",
    # Glyphs
    "spinner_frame",
    "working_harness_style",
    "_append_working_harness_text",
    "_append_working_harness_parts",
    "_status_glyph",
    "_id_style",
    "_rail_above",
    "_overlay_piece",
    "_overlay_row",
    "node_label",
    # Routing
    "AwaitCell",
    "_rail_leaves",
    "_rail_cells",
    "_route",
    "send_path",
    "await_path",
    "tree_glyph_at",
    "_await_route",
    "await_highlight_cells",
    "cell_leaf",
]
