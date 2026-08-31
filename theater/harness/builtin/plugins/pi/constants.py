"""Pi-native names and bounded adapter values."""

from __future__ import annotations

PI_BINARY = "pi"
PI_SESSIONS_DIRNAME = "sessions"
PI_ISOLATION_MARKER = ".theater-pi-source"
PI_MARKER_KEY = "pi-domain-marker.key"
PI_MARKER_VERSION = 1

# Keep one observer poll small even if an untrusted session file is damaged.
PI_READ_BYTES = 256 * 1024
PI_RECORD_BYTES = 64 * 1024
PI_RECORDS_PER_BATCH = 128
PI_HEADER_BYTES = 64 * 1024
