"""Immutable trajectory limits shared by the domain and its clients."""

from __future__ import annotations

# Maximum encoded bytes in one displayed detail field.
TRAJECTORY_DETAIL_FIELD_MAX_BYTES = 16 * 1024
# Maximum encoded detail bytes retained by one record.
TRAJECTORY_DETAIL_RECORD_MAX_BYTES = 32 * 1024
# Maximum records requested in one trajectory page or follow batch.
TRAJECTORY_PAGE_RECORD_LIMIT = 200
# Maximum encoded bytes in one trajectory response.
TRAJECTORY_RESPONSE_MAX_BYTES = 1 << 20
# Maximum cached encoded bytes for one participant stream.
TRAJECTORY_PARTICIPANT_CACHE_MAX_BYTES = 4 << 20
# Maximum cached encoded bytes across all participant streams.
TRAJECTORY_TOTAL_CACHE_MAX_BYTES = 32 << 20
# Maximum warm participant streams retained by the daemon.
TRAJECTORY_WARM_STREAM_LIMIT = 8
# Idle lifetime of a warm participant stream in seconds.
TRAJECTORY_IDLE_TTL_SECONDS = 5 * 60.0
# Maximum records retained in one régie participant window.
TRAJECTORY_UI_RECORD_LIMIT = 2_000
# Maximum encoded bytes retained in one régie participant window.
TRAJECTORY_UI_MAX_BYTES = 8 << 20
# Maximum mutable-update coalescing interval in milliseconds.
TRAJECTORY_MUTABLE_UPDATE_COALESCE_MS = 50
# Maximum server-side follow wait in seconds.
TRAJECTORY_FOLLOW_TIMEOUT_SECONDS = 20.0
# Pointer tooltip delay in milliseconds.
TRAJECTORY_TOOLTIP_DELAY_MS = 150
# Default trajectory inspector height ratio.
TRAJECTORY_INSPECTOR_RATIO_DEFAULT = 0.35
# Minimum trajectory inspector height ratio.
TRAJECTORY_INSPECTOR_RATIO_MIN = 0.20
# Maximum trajectory inspector height ratio.
TRAJECTORY_INSPECTOR_RATIO_MAX = 0.75

__all__ = [
    "TRAJECTORY_DETAIL_FIELD_MAX_BYTES",
    "TRAJECTORY_DETAIL_RECORD_MAX_BYTES",
    "TRAJECTORY_FOLLOW_TIMEOUT_SECONDS",
    "TRAJECTORY_IDLE_TTL_SECONDS",
    "TRAJECTORY_INSPECTOR_RATIO_DEFAULT",
    "TRAJECTORY_INSPECTOR_RATIO_MAX",
    "TRAJECTORY_INSPECTOR_RATIO_MIN",
    "TRAJECTORY_MUTABLE_UPDATE_COALESCE_MS",
    "TRAJECTORY_PAGE_RECORD_LIMIT",
    "TRAJECTORY_PARTICIPANT_CACHE_MAX_BYTES",
    "TRAJECTORY_RESPONSE_MAX_BYTES",
    "TRAJECTORY_TOOLTIP_DELAY_MS",
    "TRAJECTORY_TOTAL_CACHE_MAX_BYTES",
    "TRAJECTORY_UI_MAX_BYTES",
    "TRAJECTORY_UI_RECORD_LIMIT",
    "TRAJECTORY_WARM_STREAM_LIMIT",
]
