"""Contracts and constants for the Vibe unified-session store."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STORE_FORMAT = "mistral.vibe.unified-session-store/v1"
# Highest store minor understood (2: projection_delta; 4: chunk-pool transcripts; 5-7 opaque).
# The field defaults to 1, so a pre-minor pointer restores as minor 1.
STORE_FORMAT_MINOR = 7

_CHUNKS_DIRNAME = "chunks"
_GENERATION_PATTERN = re.compile(r"^[0-9]{16}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$"
)
_JOURNAL_PATH_PATTERN = re.compile(r"^journal/[0-9]{16}\.jsonl$")
_CHECKPOINT_MESSAGES_PATH = ("context", "messages")
_PROJECTION_HISTORY_PATH = ("snapshot", "history", "entries")
_MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
_MAX_SAFE_JSON_INTEGER = 2**53 - 1
_CANONICAL_INTEGER_RANGE = range(-_MAX_SAFE_JSON_INTEGER, _MAX_SAFE_JSON_INTEGER + 1)

_READER_CHUNK_CACHE_BYTES = 8 * 1024 * 1024
_JOURNAL_RECORD_TYPES = frozenset(
    {
        "command_reserved",
        "core_input",
        "action_intent",
        "action_result",
        "process_operation_dispatched",
        "process_state_changed",
        "process_notification_submitted",
        "callback_registered",
        "callback_resolved",
        "receipt_succeeded",
        "receipt_failed",
        "projection_advanced",
        "projection_delta",
    }
)
_SESSION_STATUS_TYPES = frozenset({"idle", "running", "blocked", "failed", "archived"})
_TURN_STATUSES = frozenset({"in_progress", "completed", "failed", "interrupted"})


class UnifiedStoreError(Exception):
    """A unified session store that cannot be read as it was written.

    ``code`` is stable across releases so callers can branch on it;
    ``message`` explains the specific broken invariant.
    """

    code = "vibe_unified_store_invalid"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnifiedStoreRequiresNewer(UnifiedStoreError):
    """The store's ``store_format_minor`` exceeds what this reader understands.

    Reading on would mean accepting unknown pointer fields and unknown record
    shapes, so the caller must upgrade before this store can be restored.
    """

    code = "vibe_unified_store_newer"

    def __init__(self, store_minor: int) -> None:
        super().__init__(
            f"the unified session store requires a newer reader "
            f"(store_format_minor {store_minor} > {STORE_FORMAT_MINOR}); "
            f"upgrade Theater to read it"
        )
        self.store_minor = store_minor


@dataclass(frozen=True, slots=True)
class UnifiedStoreView:
    """One exact, validated projection of a unified session store.

    ``sequence`` is the last applied record's (else ``snapshot_sequence``); ``journal_fingerprint``
    names the applied prefix by digest chain.
    """

    current: Path
    session_id: str
    store_minor: int
    generation: str
    snapshot_sequence: int
    sequence: int
    watermark: int
    snapshot: dict[str, Any]
    runtime_state: dict[str, Any]
    manifest_created_at: str
    journal_fingerprint: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class _CurrentPointer:
    session_id: str
    store_minor: int
    generation: str
    snapshot_sequence: int
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class _StoredFile:
    path: str
    sha256: str
    chunks: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class _ProjectionDocument:
    session_id: str
    snapshot_sequence: int
    watermark: int
    snapshot: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Manifest:
    session_id: str
    generation: str
    created_at: str
    snapshot_sequence: int
    execution_state: str
    checkpoint: _StoredFile
    runtime_state: _StoredFile
    projection_state: _StoredFile
    interop_export: _StoredFile | None
    journal_path: str
    first_sequence: int


@dataclass(frozen=True, slots=True)
class _JournalDetail:
    """A whole-journal read plus the facts an incremental reader pins to."""

    records: tuple[_JournalRecord, ...]
    complete_bytes: int
    last_start: int
    stat_size: int
    stat_mtime_ns: int


@dataclass(frozen=True, slots=True)
class _JournalRecord:
    sequence: int
    record_sha256: str
    record_type: str
    previous_record_sha256: str | None = None
    watermark: int | None = None
    snapshot: dict[str, Any] | None = None
    delta: tuple[dict[str, Any], ...] | None = None
