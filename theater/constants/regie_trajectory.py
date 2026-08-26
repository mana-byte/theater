"""Fixed Régie trajectory presentation constants."""

from theater.constants.trajectory import (
    TRAJECTORY_DETAIL_FIELD_MAX_BYTES,
    TRAJECTORY_TOOLTIP_DELAY_MS,
    TRAJECTORY_UI_RECORD_LIMIT,
)

# Reserve footer and breadcrumb rows in the trajectory layout.
TRAJECTORY_FOOTER_HEIGHT = 2
TRAJECTORY_BREADCRUMB_HEIGHT = 3
# Size and refresh the compact trajectory overview strip.
TRAJECTORY_OVERVIEW_HEIGHT = 3
TRAJECTORY_OVERVIEW_TICK_SECONDS = 1.0
TRAJECTORY_OVERVIEW_MILLISECONDS_PER_SECOND = 1_000
TRAJECTORY_OVERVIEW_SECONDS_PER_MINUTE = 60
TRAJECTORY_OVERVIEW_MINUTES_PER_HOUR = 60
TRAJECTORY_OVERVIEW_COMPACT_NUMBER_THRESHOLD = 1_000
# Switch footer controls at these available widths.
TRAJECTORY_FOOTER_COMPACT_WIDTH = 82
TRAJECTORY_FOOTER_NARROW_WIDTH = 58
# Keep the trajectory surface flush with its container.
TRAJECTORY_HORIZONTAL_PADDING = 0
# Define the timeline's fixed vertical and horizontal grid.
TIMELINE_HEIGHT = 10
TIMELINE_LABEL_WIDTH = 11
TIMELINE_LABEL_RIGHT_PADDING = 2
TIMELINE_LANE_HEIGHT = 2
TIMELINE_SPAN_GUTTER = 1
TIMELINE_SPAN_MIN_WIDTH = 5
TIMELINE_DURATION_MIN_WIDTH = 24
TIMELINE_DURATION_UNTIMED_GAP = 2
TIMELINE_CONTENT_HEIGHT = 8
# Draw timeline span boundaries and relationship states consistently.
TIMELINE_TURN_BOUNDARY_GLYPH = "│"
TIMELINE_HOVER_LEFT_GLYPH = "▏"
TIMELINE_HOVER_RIGHT_GLYPH = "▕"
TIMELINE_HOVER_SINGLE_GLYPH = "◆"
TIMELINE_RELATED_GLYPH = "·"
# Limit hover-card width to prevent it obscuring the timeline.
TIMELINE_HOVER_CARD_MAX_WIDTH = 80
# Mark requests in timeline and ledger summaries.
TRAJECTORY_REQUEST_POSITION_GLYPH = "↗"
# Bound diagnostics and normalize shared table row geometry.
TRAJECTORY_INSIGHT_ROW_LIMIT = 256
TRAJECTORY_INSIGHT_HEADER_HEIGHT = 2
TRAJECTORY_TABLE_CELL_PADDING = 1
TRAJECTORY_SPAN_ROW_HEIGHT = 1
TRAJECTORY_HOVERED_SPAN_ROW_HEIGHT = 2
TRAJECTORY_AUXILIARY_ROW_HEIGHT = 2
# Render compact resource intensity bars in diagnostic tables.
TRAJECTORY_RESOURCE_HEAT_WIDTH = 8
TRAJECTORY_RESOURCE_HEAT_GLYPH = "█"
# Bound waterfall nesting and per-request projection rows.
WATERFALL_BAR_WIDTH = 24
WATERFALL_MAX_DEPTH = 8
WATERFALL_ROWS_PER_REQUEST = 64
# Recognize structured tool fields that contain file paths.
TRAJECTORY_FILE_PATH_KEYS = frozenset(
    {
        "destination",
        "destination_path",
        "file",
        "file_path",
        "filepath",
        "files",
        "path",
        "paths",
        "source_file",
        "source_path",
        "target_file",
        "target_path",
    }
)
# Classify tool names conservatively for file-activity presentation.
TRAJECTORY_TOOL_READ_HINTS = ("cat", "fetch", "get", "open", "read", "view")
TRAJECTORY_TOOL_WRITE_HINTS = (
    "create",
    "delete",
    "edit",
    "move",
    "patch",
    "remove",
    "rename",
    "replace",
    "save",
    "write",
)
# Reserve rows for search and filter controls.
SEARCH_HEIGHT = 3
FILTER_MAX_ROWS = 12
FILTER_HEADER_HEIGHT = 3
# Tune ledger virtualization, density, and compact-layout thresholds.
LEDGER_OVERSCAN_ROWS = 4
LEDGER_DEFAULT_VIEWPORT_ROWS = 12
LEDGER_COMPACT_WIDTH = 64
LEDGER_MIN_SUMMARY_WIDTH = 8
LEDGER_STATUS_COLUMN_WIDTH = 16
LEDGER_DURATION_COLUMN_WIDTH = 8
# Keep tool-row summaries and selected input details readable.
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
# Reserve ledger chrome and derive bounded UI helper limits.
LEDGER_SCROLLBAR_WIDTH = 1
LEDGER_HEADER_HEIGHT = 3
MAX_SEARCH_CACHE_ENTRIES = TRAJECTORY_UI_RECORD_LIMIT * 4
MAX_QUERY_BYTES = TRAJECTORY_DETAIL_FIELD_MAX_BYTES
TRAJECTORY_NAVIGATION_HISTORY_LIMIT = 24
TOOLTIP_DELAY = TRAJECTORY_TOOLTIP_DELAY_MS / 1000

# Map wire lane values to compact timeline glyphs.
LANE_GLYPHS_BY_VALUE = {
    "input": "›",
    "model": "◆",
    "tools": "⚙",
    "theater": "◇",
}

# Map wire record kinds to compact ledger and timeline glyphs.
KIND_GLYPHS_BY_VALUE = {
    "user": "›",
    "assistant": "◆",
    "reasoning": "∴",
    "usage": "∑",
    "tool_call": "⚙",
    "tool_result": "✓",
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

# Style matched search text and durations without adding visual weight.
STYLE_MATCHED = ""
STYLE_DURATION = "bold dim"

__all__ = [
    "FILTER_HEADER_HEIGHT",
    "FILTER_MAX_ROWS",
    "KIND_GLYPHS_BY_VALUE",
    "LANE_GLYPHS_BY_VALUE",
    "LEDGER_COMPACT_WIDTH",
    "LEDGER_DEFAULT_VIEWPORT_ROWS",
    "LEDGER_DURATION_COLUMN_WIDTH",
    "LEDGER_HEADER_HEIGHT",
    "LEDGER_MIN_SUMMARY_WIDTH",
    "LEDGER_OVERSCAN_ROWS",
    "LEDGER_SCROLLBAR_WIDTH",
    "LEDGER_STATUS_COLUMN_WIDTH",
    "MAX_QUERY_BYTES",
    "MAX_SEARCH_CACHE_ENTRIES",
    "SEARCH_HEIGHT",
    "STYLE_DURATION",
    "STYLE_MATCHED",
    "TIMELINE_CONTENT_HEIGHT",
    "TIMELINE_DURATION_MIN_WIDTH",
    "TIMELINE_DURATION_UNTIMED_GAP",
    "TIMELINE_HEIGHT",
    "TIMELINE_HOVER_CARD_MAX_WIDTH",
    "TIMELINE_HOVER_LEFT_GLYPH",
    "TIMELINE_HOVER_RIGHT_GLYPH",
    "TIMELINE_HOVER_SINGLE_GLYPH",
    "TIMELINE_LABEL_RIGHT_PADDING",
    "TIMELINE_LABEL_WIDTH",
    "TIMELINE_LANE_HEIGHT",
    "TIMELINE_RELATED_GLYPH",
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
    "TRAJECTORY_AUXILIARY_ROW_HEIGHT",
    "TRAJECTORY_BREADCRUMB_HEIGHT",
    "TRAJECTORY_FILE_PATH_KEYS",
    "TRAJECTORY_FOOTER_COMPACT_WIDTH",
    "TRAJECTORY_FOOTER_HEIGHT",
    "TRAJECTORY_FOOTER_NARROW_WIDTH",
    "TRAJECTORY_HORIZONTAL_PADDING",
    "TRAJECTORY_HOVERED_SPAN_ROW_HEIGHT",
    "TRAJECTORY_INSIGHT_HEADER_HEIGHT",
    "TRAJECTORY_INSIGHT_ROW_LIMIT",
    "TRAJECTORY_NAVIGATION_HISTORY_LIMIT",
    "TRAJECTORY_OVERVIEW_COMPACT_NUMBER_THRESHOLD",
    "TRAJECTORY_OVERVIEW_HEIGHT",
    "TRAJECTORY_OVERVIEW_MILLISECONDS_PER_SECOND",
    "TRAJECTORY_OVERVIEW_MINUTES_PER_HOUR",
    "TRAJECTORY_OVERVIEW_SECONDS_PER_MINUTE",
    "TRAJECTORY_OVERVIEW_TICK_SECONDS",
    "TRAJECTORY_REQUEST_POSITION_GLYPH",
    "TRAJECTORY_RESOURCE_HEAT_GLYPH",
    "TRAJECTORY_RESOURCE_HEAT_WIDTH",
    "TRAJECTORY_SPAN_ROW_HEIGHT",
    "TRAJECTORY_TABLE_CELL_PADDING",
    "TRAJECTORY_TOOL_READ_HINTS",
    "TRAJECTORY_TOOL_WRITE_HINTS",
    "WATERFALL_BAR_WIDTH",
    "WATERFALL_MAX_DEPTH",
    "WATERFALL_ROWS_PER_REQUEST",
]
