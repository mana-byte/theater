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
# Format durations and compact counts.
TRAJECTORY_OVERVIEW_MILLISECONDS_PER_SECOND = 1_000
TRAJECTORY_OVERVIEW_SECONDS_PER_MINUTE = 60
TRAJECTORY_OVERVIEW_MINUTES_PER_HOUR = 60
TRAJECTORY_OVERVIEW_COMPACT_NUMBER_THRESHOLD = 1_000
# Define the timeline's fixed vertical and horizontal grid.
TIMELINE_HEIGHT = 12
TIMELINE_LABEL_WIDTH = 11
TIMELINE_LABEL_RIGHT_PADDING = 2
TIMELINE_LANE_HEIGHT = 2
TIMELINE_SPAN_GUTTER = 1
TIMELINE_SPAN_MIN_WIDTH = 5
TIMELINE_DURATION_MIN_WIDTH = 24
TIMELINE_DURATION_UNTIMED_GAP = 2
TIMELINE_CONTENT_HEIGHT = 10
# Draw timeline turn boundaries consistently.
TIMELINE_TURN_BOUNDARY_GLYPH = "│"
# Size and animate the search drawer.
SEARCH_HEIGHT = 3
TRAJECTORY_SEARCH_SLIDE_SECONDS = 0.12
TRAJECTORY_SEARCH_SLIDE_EASING = "out_cubic"
# Keep tool-row summaries and selected input details readable.
TRAJECTORY_JSON_EXPANDED_STRING_LIMIT = 24
TRAJECTORY_JSON_FORMAT_MAX_DEPTH = 12
TRAJECTORY_JSON_STRING_BLOCK_MIN_CHARS = 120
TOOL_ROW_SUMMARY_MAX_CHARS = 160
TOOL_ROW_INPUT_FIELD_LIMIT = 3
TOOL_ROW_INPUT_COMPACT_FIELD_LIMIT = 1
TOOL_ROW_INPUT_VALUE_MAX_CHARS = 72
TOOL_ROW_INPUT_DETAIL_NAMES = frozenset({"args", "arguments", "input", "parameters", "tool_input"})
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
    "TIMELINE_CONTENT_HEIGHT",
    "TIMELINE_DURATION_MIN_WIDTH",
    "TIMELINE_DURATION_UNTIMED_GAP",
    "TIMELINE_HEIGHT",
    "TIMELINE_LABEL_RIGHT_PADDING",
    "TIMELINE_LABEL_WIDTH",
    "TIMELINE_LANE_HEIGHT",
    "TIMELINE_SPAN_GUTTER",
    "TIMELINE_SPAN_MIN_WIDTH",
    "TIMELINE_TURN_BOUNDARY_GLYPH",
    "TOOLTIP_DELAY",
    "TOOL_ROW_INPUT_COMPACT_FIELD_LIMIT",
    "TOOL_ROW_INPUT_DETAIL_NAMES",
    "TOOL_ROW_INPUT_FIELD_LIMIT",
    "TOOL_ROW_INPUT_KEY_PRIORITY",
    "TOOL_ROW_INPUT_VALUE_MAX_CHARS",
    "TOOL_ROW_SUMMARY_MAX_CHARS",
    "TRAJECTORY_FOOTER_HEIGHT",
    "TRAJECTORY_HEADER_HEIGHT",
    "TRAJECTORY_JSON_EXPANDED_STRING_LIMIT",
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
