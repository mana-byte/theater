"""Bounds and presentation constants for the standalone trajectory surface."""

MAX_FIELD_BYTES = 16 * 1024
MAX_DETAIL_BYTES = 32 * 1024
MAX_PAGE_RECORDS = 200
MAX_RESPONSE_BYTES = 1 * 1024 * 1024
MAX_LOADED_RECORDS = 2_000
MAX_LOADED_BYTES = 8 * 1024 * 1024
MAX_PARTICIPANT_STATES = 8
MAX_IDENTIFIER_BYTES = 256
MAX_SOURCE_BYTES = 256
MAX_DETAIL_FIELDS = 64
MAX_LINKS = 32
MIN_INSPECTOR_RATIO = 0.20
MAX_INSPECTOR_RATIO = 0.75
DEFAULT_INSPECTOR_RATIO = 0.35
INSPECTOR_RESIZE_STEP = 0.02
INSPECTOR_SCROLL_STEP = 3
TIMELINE_PADDING = 1
TIMELINE_HEIGHT = 3
STATUS_HEIGHT = 2
SEARCH_HEIGHT = 3
FILTER_MAX_ROWS = 12
INSPECTOR_MIN_HEIGHT = 4
LEDGER_OVERSCAN_ROWS = 4
LEDGER_DEFAULT_VIEWPORT_ROWS = 12
LEDGER_SCROLL_STEP = 3
MAX_SEARCH_CACHE_ENTRIES = 8_192
MAX_QUERY_BYTES = MAX_FIELD_BYTES
TOOLTIP_DELAY = 0.150
MAX_TOOLTIP_BYTES = 2 * 1024
MAX_COPY_BYTES = MAX_DETAIL_BYTES

LANE_GLYPHS_BY_VALUE = {
    "input": "›",
    "model": "◆",
    "tools": "⚙",
    "theater": "◇",
    "unknown": "?",
}

KIND_GLYPHS_BY_VALUE = {
    "turn": "↻",
    "step": "·",
    "user": "›",
    "assistant": "◆",
    "reasoning": "∴",
    "tool_call": "⚙",
    "tool_result": "✓",
    "system": "§",
    "context_change": "⇄",
    "spawn": "＋",
    "resume": "↺",
    "send": "→",
    "receive": "←",
    "await_start": "…",
    "await_end": "✓",
    "kill": "×",
    "job_failure": "!",
    "session_boundary": "║",
    "observation_error": "!",
    "unknown": "?",
}

STYLE_MATCHED = ""
STYLE_UNMATCHED = "dim"
STYLE_HOVERED = "reverse bold"
STYLE_SELECTED = "bold underline"
STYLE_DURATION = "cyan"
STYLE_FILTER_CURSOR = "reverse bold"
