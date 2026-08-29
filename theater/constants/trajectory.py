"""Immutable trajectory limits shared by the domain and its clients."""

from __future__ import annotations

# Maximum encoded bytes in one displayed detail field.
TRAJECTORY_DETAIL_FIELD_MAX_BYTES = 16 * 1024
# Maximum encoded detail bytes retained by one record.
TRAJECTORY_DETAIL_RECORD_MAX_BYTES = 32 * 1024
# Maximum encoded bytes in one identifier.
TRAJECTORY_IDENTIFIER_MAX_BYTES = 512
# Maximum encoded bytes in one source label.
TRAJECTORY_SOURCE_MAX_BYTES = 256
# Maximum encoded bytes in one opaque trajectory cursor.
TRAJECTORY_CURSOR_MAX_BYTES = 4 * 1024
# Maximum encoded bytes in one detail name.
TRAJECTORY_DETAIL_NAME_MAX_BYTES = 256
# Maximum detail fields retained by one record.
TRAJECTORY_MAX_DETAILS_PER_RECORD = 64
# Maximum participant links retained by one record.
TRAJECTORY_MAX_LINKS_PER_RECORD = 64
# Maximum record ids retained by one group.
TRAJECTORY_MAX_GROUP_RECORD_IDS = 200
# Maximum child groups retained by one group.
TRAJECTORY_MAX_GROUP_CHILDREN = 200
# Maximum groups retained by one page.
TRAJECTORY_MAX_PAGE_GROUPS = 200
# Maximum coverage gaps retained by one page.
TRAJECTORY_MAX_COVERAGE_GAPS = 64
# Maximum records requested in one trajectory page or follow batch.
TRAJECTORY_PAGE_RECORD_LIMIT = 200
# Maximum encoded bytes in one trajectory response.
TRAJECTORY_RESPONSE_MAX_BYTES = 1 << 20
# Largest signed 64-bit request id used to size trajectory response envelopes.
TRAJECTORY_RESPONSE_SIZING_REQUEST_ID = -(1 << 63)
# Maximum encoded bytes in a compact trajectory overview summary.
TRAJECTORY_OVERVIEW_SUMMARY_MAX_BYTES = 1024
# Maximum count retained in one trajectory overview aggregate.
TRAJECTORY_OVERVIEW_MAX_COUNT = (1 << 31) - 1
# Maximum token total retained in one trajectory overview aggregate.
TRAJECTORY_OVERVIEW_MAX_TOKENS = (1 << 63) - 1
# Maximum reported USD cost retained in one trajectory overview aggregate.
TRAJECTORY_OVERVIEW_MAX_COST_USD = 1e15
# Maximum active milliseconds retained in one trajectory overview aggregate.
TRAJECTORY_OVERVIEW_MAX_DURATION_MS = 100 * 365 * 24 * 60 * 60 * 1000
# Maximum cached encoded bytes for one participant stream.
TRAJECTORY_PARTICIPANT_CACHE_MAX_BYTES = 4 << 20
# Maximum cached encoded bytes across all participant streams.
TRAJECTORY_TOTAL_CACHE_MAX_BYTES = 32 << 20
# Maximum warm participant streams retained by the daemon.
TRAJECTORY_WARM_STREAM_LIMIT = 8
# Maximum opaque older-page cursors retained per warm stream.
TRAJECTORY_OLDER_CURSOR_LIMIT = 256
# Idle lifetime of a warm participant stream in seconds.
TRAJECTORY_IDLE_TTL_SECONDS = 5 * 60.0
# Interval between idle trajectory cache sweeps.
TRAJECTORY_CACHE_SWEEP_SECONDS = 30.0
# Maximum bus rows projected before yielding back to the event loop.
TRAJECTORY_BUS_DRAIN_BATCH = 256
# Stable identity of records projected from the daemon coordination bus.
TRAJECTORY_THEATER_BUS_RECORD_PREFIX = "bus:"
TRAJECTORY_THEATER_BUS_SOURCE_EPOCH = "theater-bus"
# Maximum persisted observation rows used to estimate transcript timing.
TRAJECTORY_OBSERVATION_TIMING_ROW_LIMIT = 800
# Maximum remembered MCP call identities used to classify result-only records.
TRAJECTORY_MCP_CALL_CONTEXT_LIMIT = 2_000
# Maximum records retained in one régie participant window.
TRAJECTORY_UI_RECORD_LIMIT = 2_000
# Maximum record links retained by one request projection.
TRAJECTORY_REQUEST_RECORD_LIMIT = TRAJECTORY_UI_RECORD_LIMIT
# Maximum record links retained by one tool operation projection.
TRAJECTORY_TOOL_RECORD_LIMIT = TRAJECTORY_UI_RECORD_LIMIT
# Maximum encoded bytes retained in one régie participant window.
TRAJECTORY_UI_MAX_BYTES = 8 << 20
# Default number of records shown on one trajectory ledger page.
TRAJECTORY_LEDGER_PAGE_SIZE_DEFAULT = 30
# Maximum configurable trajectory ledger page size.
TRAJECTORY_LEDGER_PAGE_SIZE_MAX = TRAJECTORY_UI_RECORD_LIMIT
# Byte window read backwards for one transcript history page.
TRAJECTORY_TRANSCRIPT_HISTORY_WINDOW_BYTES = 256 * 1024
# Hard cap on one transcript history page reverse scan.
TRAJECTORY_TRANSCRIPT_HISTORY_MAX_SCAN_BYTES = 1 << 20
# Bytes sampled at each end of a transcript cursor identity.
TRAJECTORY_TRANSCRIPT_CURSOR_FINGERPRINT_BYTES = 64
# Maximum mutable-update coalescing interval in milliseconds.
TRAJECTORY_MUTABLE_UPDATE_COALESCE_MS = 50
# Maximum server-side follow wait in seconds.
TRAJECTORY_FOLLOW_TIMEOUT_SECONDS = 20.0
# Pointer tooltip delay in milliseconds.
TRAJECTORY_TOOLTIP_DELAY_MS = 50
# Maximum terminal cells shown from a record summary in a timeline tooltip.
TRAJECTORY_TOOLTIP_SUMMARY_MAX_CELLS = 240
__all__ = [
    "TRAJECTORY_BUS_DRAIN_BATCH",
    "TRAJECTORY_CACHE_SWEEP_SECONDS",
    "TRAJECTORY_CURSOR_MAX_BYTES",
    "TRAJECTORY_DETAIL_FIELD_MAX_BYTES",
    "TRAJECTORY_DETAIL_NAME_MAX_BYTES",
    "TRAJECTORY_DETAIL_RECORD_MAX_BYTES",
    "TRAJECTORY_FOLLOW_TIMEOUT_SECONDS",
    "TRAJECTORY_IDENTIFIER_MAX_BYTES",
    "TRAJECTORY_IDLE_TTL_SECONDS",
    "TRAJECTORY_LEDGER_PAGE_SIZE_DEFAULT",
    "TRAJECTORY_LEDGER_PAGE_SIZE_MAX",
    "TRAJECTORY_MAX_COVERAGE_GAPS",
    "TRAJECTORY_MAX_DETAILS_PER_RECORD",
    "TRAJECTORY_MAX_GROUP_CHILDREN",
    "TRAJECTORY_MAX_GROUP_RECORD_IDS",
    "TRAJECTORY_MAX_LINKS_PER_RECORD",
    "TRAJECTORY_MAX_PAGE_GROUPS",
    "TRAJECTORY_MCP_CALL_CONTEXT_LIMIT",
    "TRAJECTORY_MUTABLE_UPDATE_COALESCE_MS",
    "TRAJECTORY_OBSERVATION_TIMING_ROW_LIMIT",
    "TRAJECTORY_OLDER_CURSOR_LIMIT",
    "TRAJECTORY_OVERVIEW_MAX_COST_USD",
    "TRAJECTORY_OVERVIEW_MAX_COUNT",
    "TRAJECTORY_OVERVIEW_MAX_DURATION_MS",
    "TRAJECTORY_OVERVIEW_MAX_TOKENS",
    "TRAJECTORY_OVERVIEW_SUMMARY_MAX_BYTES",
    "TRAJECTORY_PAGE_RECORD_LIMIT",
    "TRAJECTORY_PARTICIPANT_CACHE_MAX_BYTES",
    "TRAJECTORY_REQUEST_RECORD_LIMIT",
    "TRAJECTORY_RESPONSE_MAX_BYTES",
    "TRAJECTORY_RESPONSE_SIZING_REQUEST_ID",
    "TRAJECTORY_SOURCE_MAX_BYTES",
    "TRAJECTORY_THEATER_BUS_RECORD_PREFIX",
    "TRAJECTORY_THEATER_BUS_SOURCE_EPOCH",
    "TRAJECTORY_TOOLTIP_DELAY_MS",
    "TRAJECTORY_TOOLTIP_SUMMARY_MAX_CELLS",
    "TRAJECTORY_TOOL_RECORD_LIMIT",
    "TRAJECTORY_TOTAL_CACHE_MAX_BYTES",
    "TRAJECTORY_TRANSCRIPT_CURSOR_FINGERPRINT_BYTES",
    "TRAJECTORY_TRANSCRIPT_HISTORY_MAX_SCAN_BYTES",
    "TRAJECTORY_TRANSCRIPT_HISTORY_WINDOW_BYTES",
    "TRAJECTORY_UI_MAX_BYTES",
    "TRAJECTORY_UI_RECORD_LIMIT",
    "TRAJECTORY_WARM_STREAM_LIMIT",
]
