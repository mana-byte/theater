"""Fixed Régie trajectory presentation constants."""

from regie.trajectory.limits import (
    TRAJECTORY_SEARCH_QUERY_MAX_BYTES,
    TRAJECTORY_TOOLTIP_DELAY_MS,
    TRAJECTORY_UI_RECORD_LIMIT,
)

# Header (one padded status line) and key-hint footer rows.
TRAJECTORY_HEADER_HEIGHT = 3
TRAJECTORY_FOOTER_HEIGHT = 1
# Refresh live durations and debounce full-history search.
TRAJECTORY_OVERVIEW_TICK_SECONDS = 1.0
TRAJECTORY_SEARCH_DEBOUNCE_SECONDS = 0.15
# Live updates refresh details quickly; cursor moves wait until the cursor rests.
TRAJECTORY_DETAIL_SYNC_SECONDS = 0.06
TRAJECTORY_DETAIL_SETTLE_SECONDS = 0.25
# Format durations and compact counts.
TRAJECTORY_OVERVIEW_MILLISECONDS_PER_SECOND = 1_000
TRAJECTORY_OVERVIEW_SECONDS_PER_MINUTE = 60
TRAJECTORY_OVERVIEW_MINUTES_PER_HOUR = 60
TRAJECTORY_OVERVIEW_COMPACT_NUMBER_THRESHOLD = 1_000
# Timeline grid: a lane's bar rows sit one blank row apart; lanes sit two apart.
TIMELINE_HEIGHT = 11
TIMELINE_LABEL_WIDTH = 12
TIMELINE_LABEL_RIGHT_PADDING = 3
# Concurrent spans stack into extra rows of their lane, up to this many.
TIMELINE_MAX_LANE_ROWS = 4
# Idle time between activity collapses to this many cells.
TIMELINE_IDLE_GAP_CELLS = 2
TIMELINE_SCALE_SEARCH_STEPS = 24
TIMELINE_SCROLL_STEP = 4
# Default zoom gives the median span this many cells; +/- double or halve it.
TIMELINE_TARGET_SPAN_CELLS = 5
TIMELINE_SPAN_MIN_CELLS = 3
TIMELINE_ZOOM_STEP = 2.0
TIMELINE_ZOOM_MIN = 1 / 64
TIMELINE_ZOOM_MAX = 64.0
# Span bars have a visible start and end; instants are a single marker.
TIMELINE_GLYPH_START = "┣"
TIMELINE_GLYPH_BODY = "━"
TIMELINE_GLYPH_END = "┫"
TIMELINE_GLYPH_POINT = "◆"
TIMELINE_GLYPH_TURN = "┊"
TIMELINE_GLYPH_RAIL = "─"
# Fixed lane hues: theme roles collide (textual-dark's accent and warning match).
TIMELINE_LANE_COLORS = {
    "input": "#60A5FA",
    "model": "#C084FC",
    "tools": "#FBBF24",
    "mcp": "#34D399",
    "theater": "#F472B6",
}
# Size and animate the search drawer.
SEARCH_HEIGHT = 3
TRAJECTORY_SEARCH_SLIDE_SECONDS = 0.12
TRAJECTORY_SEARCH_SLIDE_EASING = "out_cubic"
# Bound nested data rendering and pick a tool's headline argument.
TRAJECTORY_JSON_FORMAT_MAX_DEPTH = 12
TRAJECTORY_JSON_STRING_BLOCK_MIN_CHARS = 120
TRAJECTORY_INLINE_LIST_ITEMS = 6
# Long detail sections show this many lines until expanded.
TRAJECTORY_DETAIL_FOLD_LINES = 20
# Detail section heading tints by role, blended over the theme background.
TRAJECTORY_DETAIL_ROLE_COLORS = {
    "input": "#60A5FA",
    "output": "#34D399",
    "reasoning": "#C084FC",
    "tools": "#FBBF24",
    "error": "#F87171",
    "links": "#F472B6",
    "debug": "#94A3B8",
    "other": "#94A3B8",
}
TOOL_ROW_INPUT_VALUE_MAX_CHARS = 72
TOOL_ROW_INPUT_KEY_PRIORITY = (
    "command",
    "cmd",
    "path",
    "file_path",
    "query",
    "pattern",
    "url",
    "target",
    "task",
    "prompt",
    "message",
)
# Derive bounded search and navigation limits.
MAX_SEARCH_CACHE_ENTRIES = TRAJECTORY_UI_RECORD_LIMIT * 4
MAX_QUERY_BYTES = TRAJECTORY_SEARCH_QUERY_MAX_BYTES
TRAJECTORY_NAVIGATION_HISTORY_LIMIT = 24
TOOLTIP_DELAY = TRAJECTORY_TOOLTIP_DELAY_MS / 1000


# Map wire record kinds to compact glyphs.
KIND_GLYPHS_BY_VALUE = {
    "user": "›",
    "assistant": "◆",
    "reasoning": "∴",
    "usage": "∑",
    "tool_call": "⚙",
    "tool_result": "✓",
    "theater_call": "◇",
    "theater_result": "✓",
    "error": "!",
    "system": "§",
    "context": "⇄",
    "theater": "◇",
    "spawn": "＋",
    "resume": "↺",
    "send": "→",
    "receive": "←",
    "await_start": "…",
    "await_end": "✓",
    "kill": "×",
    "job_failure": "!",
    "transcript_boundary": "║",
    "session_boundary": "║",
    "observation_error": "!",
    "unknown": "?",
}


__all__ = [
    "KIND_GLYPHS_BY_VALUE",
    "MAX_QUERY_BYTES",
    "MAX_SEARCH_CACHE_ENTRIES",
    "SEARCH_HEIGHT",
    "TIMELINE_GLYPH_BODY",
    "TIMELINE_GLYPH_END",
    "TIMELINE_GLYPH_POINT",
    "TIMELINE_GLYPH_RAIL",
    "TIMELINE_GLYPH_START",
    "TIMELINE_GLYPH_TURN",
    "TIMELINE_HEIGHT",
    "TIMELINE_IDLE_GAP_CELLS",
    "TIMELINE_LABEL_RIGHT_PADDING",
    "TIMELINE_LABEL_WIDTH",
    "TIMELINE_LANE_COLORS",
    "TIMELINE_MAX_LANE_ROWS",
    "TIMELINE_SCALE_SEARCH_STEPS",
    "TIMELINE_SCROLL_STEP",
    "TIMELINE_SPAN_MIN_CELLS",
    "TIMELINE_TARGET_SPAN_CELLS",
    "TIMELINE_ZOOM_MAX",
    "TIMELINE_ZOOM_MIN",
    "TIMELINE_ZOOM_STEP",
    "TOOLTIP_DELAY",
    "TOOL_ROW_INPUT_KEY_PRIORITY",
    "TOOL_ROW_INPUT_VALUE_MAX_CHARS",
    "TRAJECTORY_DETAIL_FOLD_LINES",
    "TRAJECTORY_DETAIL_ROLE_COLORS",
    "TRAJECTORY_DETAIL_SETTLE_SECONDS",
    "TRAJECTORY_DETAIL_SYNC_SECONDS",
    "TRAJECTORY_FOOTER_HEIGHT",
    "TRAJECTORY_HEADER_HEIGHT",
    "TRAJECTORY_INLINE_LIST_ITEMS",
    "TRAJECTORY_JSON_FORMAT_MAX_DEPTH",
    "TRAJECTORY_JSON_STRING_BLOCK_MIN_CHARS",
    "TRAJECTORY_NAVIGATION_HISTORY_LIMIT",
    "TRAJECTORY_OVERVIEW_COMPACT_NUMBER_THRESHOLD",
    "TRAJECTORY_OVERVIEW_MILLISECONDS_PER_SECOND",
    "TRAJECTORY_OVERVIEW_MINUTES_PER_HOUR",
    "TRAJECTORY_OVERVIEW_SECONDS_PER_MINUTE",
    "TRAJECTORY_OVERVIEW_TICK_SECONDS",
    "TRAJECTORY_SEARCH_DEBOUNCE_SECONDS",
    "TRAJECTORY_SEARCH_SLIDE_EASING",
    "TRAJECTORY_SEARCH_SLIDE_SECONDS",
]
