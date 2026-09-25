"""Incremental reader for the Vibe unified-session store."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .unified_store_io import _ChunkCache, _reject_symlink_components
from .unified_store_loading import (
    _load_active_detailed,
    _parse_journal_tail,
    _read_current_bytes,
    _session_root,
)
from .unified_store_types import (
    _MAX_DOCUMENT_BYTES,
    _READER_CHUNK_CACHE_BYTES,
    UnifiedStoreError,
    UnifiedStoreView,
    _JournalRecord,
    _Manifest,
)
from .unified_store_validation import _replay_projection

# --- Incremental reader --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnifiedStoreUpdate:
    """A view and its changed entry IDs relative to ``baseline``."""

    view: UnifiedStoreView
    changed_entry_ids: frozenset[str] | None
    baseline: UnifiedStoreView | None


@dataclass(slots=True)
class _ReaderState:
    """Validated state needed by the incremental path."""

    session_root: Path
    session_id: str
    current_bytes: bytes
    store_minor: int
    generation: str
    snapshot_sequence: int
    manifest: _Manifest
    runtime_value: dict[str, Any]
    journal_path: Path
    journal_size: int
    journal_mtime_ns: int
    journal_verified: int
    last_record_start: int
    last_sequence: int
    last_digest: str | None
    previous_digest: str | None
    watermark: int
    snapshot: dict[str, Any]
    journal_fingerprint: tuple[str, ...] | None
    view: UnifiedStoreView


class UnifiedStoreReader:
    """Serialized incremental reader for one unified session store."""

    def __init__(self, *, chunk_cache_bytes: int = _READER_CHUNK_CACHE_BYTES) -> None:
        self._chunks = _ChunkCache(chunk_cache_bytes)
        self._state: _ReaderState | None = None
        self._lock = threading.Lock()

    @property
    def chunk_cache(self) -> _ChunkCache:
        """The bounded chunk-body cache to thread through plain store loads."""
        return self._chunks

    @property
    def last_view(self) -> UnifiedStoreView | None:
        with self._lock:
            return None if self._state is None else self._state.view

    def load(self, current: Path) -> UnifiedStoreUpdate:
        """Load the active publication, incrementally when the cache allows it."""
        with self._lock:
            return self._load_locked(Path(current))

    def _load_locked(self, current_path: Path) -> UnifiedStoreUpdate:
        session_root = _session_root(current_path)
        session_id = session_root.name
        state = self._state
        if state is not None and (
            state.session_root != session_root or state.session_id != session_id
        ):
            self._state = None
            self._chunks.clear()
            state = None
        if state is None:
            return self._reload(session_root, session_id, current_path)
        _reject_symlink_components(session_root, current_path)
        data = _read_current_bytes(session_root)
        if data is None or data != state.current_bytes:
            return self._reload(session_root, session_id, current_path)
        _reject_symlink_components(session_root, session_root / "generations" / state.generation)
        _reject_symlink_components(session_root, state.journal_path)
        try:
            stat = os.lstat(state.journal_path)
        except OSError:
            return self._reload(session_root, session_id, current_path)
        if stat.st_size == state.journal_size and stat.st_mtime_ns == state.journal_mtime_ns:
            return UnifiedStoreUpdate(state.view, frozenset(), state.view)
        if stat.st_size < state.journal_verified or (
            stat.st_size == state.journal_verified and stat.st_mtime_ns != state.journal_mtime_ns
        ):
            # Truncated, or rewritten in place at the same size: the journal
            # is only ever appended to, so neither can be an ordinary write.
            return self._reload(session_root, session_id, current_path)
        return self._consume(state, stat, current_path)

    def _consume(
        self, state: _ReaderState, stat: os.stat_result, current_path: Path
    ) -> UnifiedStoreUpdate:
        """Fold the journal bytes appended since the last verified record in."""
        try:
            records, starts, complete, region_start = self._read_tail(state)
            # The region begins with the previously verified record when one
            # exists; the new records are everything after that pinned one.
            new_records = records if state.last_digest is None else records[1:]
        except (OSError, UnifiedStoreError, ValueError):
            return self._reload(state.session_root, state.session_id, current_path)
        if not new_records:
            # The store has not advanced past the last verified record — the
            # tail is still torn, or only non-projection bytes have landed.
            state.journal_size = stat.st_size
            state.journal_mtime_ns = stat.st_mtime_ns
            return UnifiedStoreUpdate(state.view, frozenset(), state.view)
        try:
            changed, unknown, watermark, snapshot = self._replay(state, new_records)
        except UnifiedStoreError:
            return self._reload(state.session_root, state.session_id, current_path)
        sequence = new_records[-1].sequence
        prior_fingerprint = state.journal_fingerprint or ()
        fingerprint = (*prior_fingerprint, *(r.record_sha256 for r in new_records))
        baseline = state.view
        view = UnifiedStoreView(
            current=current_path,
            session_id=state.session_id,
            store_minor=state.store_minor,
            generation=state.generation,
            snapshot_sequence=state.snapshot_sequence,
            sequence=sequence,
            watermark=watermark,
            snapshot=snapshot,
            runtime_state=state.runtime_value,
            manifest_created_at=state.manifest.created_at,
            journal_fingerprint=fingerprint or None,
        )
        state.previous_digest = (
            new_records[-2].record_sha256 if len(new_records) >= 2 else state.last_digest
        )
        state.view = view
        state.watermark = watermark
        state.snapshot = snapshot
        state.journal_fingerprint = fingerprint or None
        state.journal_verified = region_start + complete
        state.last_record_start = region_start + starts[-1]
        state.last_sequence = sequence
        state.last_digest = new_records[-1].record_sha256
        state.journal_size = stat.st_size
        state.journal_mtime_ns = stat.st_mtime_ns
        return UnifiedStoreUpdate(view, None if unknown else frozenset(changed), baseline)

    def _read_tail(self, state: _ReaderState) -> tuple[Any, Any, int, int]:
        """Read from the last verified journal record through the current tail."""
        region_start = state.last_record_start
        with state.journal_path.open("rb") as stream:
            stream.seek(region_start)
            data = stream.read(_MAX_DOCUMENT_BYTES + 1)
        if len(data) > _MAX_DOCUMENT_BYTES:
            raise UnifiedStoreError("recovery journal is too large")
        if state.last_digest is None:
            records, starts, complete = _parse_journal_tail(
                data, first_sequence=state.manifest.first_sequence, previous_digest=None
            )
        else:
            records, starts, complete = _parse_journal_tail(
                data,
                first_sequence=state.last_sequence,
                previous_digest=state.previous_digest,
                pin=state.last_digest,
            )
        return records, starts, complete, region_start

    def _replay(
        self, state: _ReaderState, records: tuple[_JournalRecord, ...]
    ) -> tuple[set[str], bool, int, dict[str, Any]]:
        """Replay records and report touched entry IDs when knowable."""
        changed: set[str] = set()
        unknown = False
        watermark = state.watermark
        snapshot = state.snapshot
        for record in records:
            if record.watermark is not None:
                if record.record_type == "projection_delta":
                    for op in record.delta or ():
                        kind = op.get("op")
                        if kind in ("replace_entry", "remove_entry"):
                            entry_id: Any = op["id"]
                        elif kind == "append_entry":
                            entry_id = op["entry"].get("id")
                        else:
                            entry_id = None
                            if kind == "set_history_entries":
                                unknown = True
                        if isinstance(entry_id, str) and entry_id:
                            changed.add(entry_id)
                        elif entry_id is not None:
                            unknown = True
                else:
                    unknown = True
            watermark, snapshot = _replay_projection(watermark, snapshot, (record,))
        return changed, unknown, watermark, snapshot

    def _reload(
        self, session_root: Path, session_id: str, current_path: Path
    ) -> UnifiedStoreUpdate:
        """Full load through the authority path, rebuilding the cached state."""
        view, report = _load_active_detailed(session_root, session_id, current_path, self._chunks)
        self._state = self._state_from(session_root, session_id, view, report)
        return UnifiedStoreUpdate(view, None, None)

    @staticmethod
    def _state_from(
        session_root: Path, session_id: str, view: UnifiedStoreView, report: dict[str, Any]
    ) -> _ReaderState:
        records: tuple[_JournalRecord, ...] = report["journal_records"]
        manifest: _Manifest = report["manifest"]
        return _ReaderState(
            session_root=session_root,
            session_id=session_id,
            current_bytes=report["current_bytes"],
            store_minor=view.store_minor,
            generation=view.generation,
            snapshot_sequence=view.snapshot_sequence,
            manifest=manifest,
            runtime_value=report["runtime_value"],
            journal_path=report["journal_path"],
            journal_size=report["journal_size"],
            journal_mtime_ns=report["journal_mtime_ns"],
            journal_verified=report["journal_complete_bytes"],
            last_record_start=report["journal_last_start"],
            last_sequence=records[-1].sequence if records else manifest.snapshot_sequence,
            last_digest=records[-1].record_sha256 if records else None,
            previous_digest=records[-2].record_sha256 if len(records) >= 2 else None,
            watermark=view.watermark,
            snapshot=view.snapshot,
            journal_fingerprint=view.journal_fingerprint,
            view=view,
        )
