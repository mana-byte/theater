"""Compatibility façade re-exporting the route animation surface.

Implementation lives in :mod:`theater.regie.animations.routes`.
"""

from __future__ import annotations

# ruff: noqa: F401, I001
from theater.regie.animations.routes import (
    AwaitRouteAnim,
    LeafOverlay,
    RouteAnim,
    RouteAnimationController,
    StartAwaitDecision,
    StartRouteDecision,
    StopAwaitDecision,
    TickResult,
    _AWAIT_TRACE_GLYPHS,
    _RAIL_ARMS,
    _SEND_TRACE_GLYPHS,
    _await_route_glyph,
    _await_route_style,
    _send_trace_glyph,
)
