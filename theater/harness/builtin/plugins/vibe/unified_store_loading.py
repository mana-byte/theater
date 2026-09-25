"""Publication loading for the Vibe unified-session store."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .unified_store_io import (
    _attach_transcript,
    _canonical_json,
    _ChunkCache,
    _decode_json,
    _read_canonical_document,
    _read_chunked_transcript,
    _read_referenced_document,
    _reject_symlink,
    _reject_symlink_components,
    _sha256,
    _sha256_json,
)
from .unified_store_types import (
    _CHECKPOINT_MESSAGES_PATH,
    _CHUNKS_DIRNAME,
    _GENERATION_PATTERN,
    _MAX_DOCUMENT_BYTES,
    _PROJECTION_HISTORY_PATH,
    _SESSION_ID_PATTERN,
    UnifiedStoreError,
    UnifiedStoreRequiresNewer,
    UnifiedStoreView,
    _CurrentPointer,
    _JournalDetail,
    _JournalRecord,
)
from .unified_store_validation import (
    _newer_minor,
    _replay_projection,
    _safe_integer,
    _validate_current,
    _validate_journal_record,
    _validate_manifest,
    _validate_projection_document,
    _validate_runtime_document,
)


def load_unified_store(
    current: Path,
    *,
    at_sequence: int | None = None,
    generation_hint: str | None = None,
    chunk_cache: _ChunkCache | None = None,
) -> UnifiedStoreView | None:
    """Read the unified session store whose pointer is ``current``.

    ``at_sequence`` returns that exact historical view or ``None``; an active load retries once
    if CURRENT moved, so a view never mixes two publications.
    """
    current_path = Path(current)
    session_root = _session_root(current_path)
    session_id = session_root.name
    if at_sequence is not None:
        _safe_integer(at_sequence, "at_sequence")
    if at_sequence is None and generation_hint is None:
        return _load_active(session_root, session_id, current_path, chunk_cache)
    return _load_historical(
        session_root, session_id, current_path, at_sequence, generation_hint, chunk_cache
    )


def current_fingerprint(current: Path) -> str | None:
    """A cheap fingerprint of the CURRENT pointer (equal means identical bytes), or ``None``."""
    try:
        session_root = _session_root(Path(current))
    except UnifiedStoreError:
        return None
    data = _read_current_bytes(session_root)
    if data is None:
        return None
    return _sha256(data)


# A load fails either because the store is broken or because the filesystem went
# away under it; a moved CURRENT is the only failure worth retrying.
_LOAD_FAILURES = (UnifiedStoreError, OSError)


def _load_active(
    session_root: Path, session_id: str, current_path: Path, chunk_cache: _ChunkCache | None = None
) -> UnifiedStoreView:
    """Load the publication CURRENT names, retrying once if the pointer moved."""
    return _load_active_detailed(session_root, session_id, current_path, chunk_cache)[0]


def _load_active_detailed(
    session_root: Path,
    session_id: str,
    current_path: Path,
    chunk_cache: _ChunkCache | None = None,
) -> tuple[UnifiedStoreView, dict[str, Any]]:
    """Load the active publication and incremental-reader metadata."""
    before = _read_current_bytes(session_root)
    report: dict[str, Any] = {}
    try:
        view = _load_current_publication(
            session_root, session_id, current_path, chunk_cache, report
        )
    except _LOAD_FAILURES as first_failure:
        return _retry_active_load(
            session_root, session_id, current_path, before, first_failure, chunk_cache
        )
    return view, report


def _pointer_moved(session_root: Path, before: bytes | None) -> bool:
    """Whether the CURRENT pointer changed since ``before`` was sampled."""
    after = _read_current_bytes(session_root)
    if before is None or after is None:
        return False
    return after != before


def _retry_active_load(
    session_root: Path,
    session_id: str,
    current_path: Path,
    before: bytes | None,
    first_failure: UnifiedStoreError | OSError,
    chunk_cache: _ChunkCache | None = None,
) -> tuple[UnifiedStoreView, dict[str, Any]]:
    """Retry an active load exactly once, when CURRENT moved underneath it.

    Only a moved pointer can explain a vanished generation; otherwise the original failure stands.
    """
    if not _pointer_moved(session_root, before):
        raise _as_store_error(first_failure) from first_failure
    report: dict[str, Any] = {}
    try:
        view = _load_current_publication(
            session_root, session_id, current_path, chunk_cache, report
        )
    except _LOAD_FAILURES as second_failure:
        raise _as_store_error(second_failure) from first_failure
    return view, report


def _load_historical(
    session_root: Path,
    session_id: str,
    current_path: Path,
    at_sequence: int | None,
    generation_hint: str | None,
    chunk_cache: _ChunkCache | None = None,
) -> UnifiedStoreView | None:
    candidates: list[str] = []
    if generation_hint is not None:
        if _GENERATION_PATTERN.fullmatch(generation_hint) is None:
            raise UnifiedStoreError(f"invalid generation hint: {generation_hint!r}")
        candidates.append(generation_hint)
    pointer = _read_pointer(session_root, session_id)
    candidates.append(pointer.generation)
    candidates.extend(_enumerate_generations(session_root))
    for generation in dict.fromkeys(candidates):
        active = generation == pointer.generation
        try:
            view = _load_generation(
                session_root,
                session_id,
                generation,
                store_minor=pointer.store_minor,
                current_path=current_path,
                at_sequence=at_sequence,
                manifest_sha256=pointer.manifest_sha256 if active else None,
                expected_snapshot_sequence=pointer.snapshot_sequence if active else None,
                chunk_cache=chunk_cache,
            )
        except OSError:
            # A retained candidate can vanish mid-search when a publication
            # collects its generation; that is an uncovering, not a corruption.
            continue
        if view is not None:
            return view
    return None


def _read_pointer(
    session_root: Path, session_id: str, report: dict[str, Any] | None = None
) -> _CurrentPointer:
    """Read, decode, and validate the CURRENT pointer.

    The newer-minor check runs first: strict validation would hide the actionable cause.
    """
    try:
        value, body = _read_canonical_document(session_root / "CURRENT", "CURRENT")
    except OSError as exc:
        raise UnifiedStoreError("the CURRENT pointer is missing or unreadable") from exc
    if report is not None:
        report["current_bytes"] = body + b"\n"
    newer_minor = _newer_minor(value)
    if newer_minor is not None:
        raise UnifiedStoreRequiresNewer(newer_minor)
    pointer = _validate_current(value)
    if pointer.session_id != session_id:
        raise UnifiedStoreError("CURRENT names another session")
    return pointer


def _load_current_publication(
    session_root: Path,
    session_id: str,
    current_path: Path,
    chunk_cache: _ChunkCache | None = None,
    report: dict[str, Any] | None = None,
) -> UnifiedStoreView:
    pointer = _read_pointer(session_root, session_id, report)
    view = _load_generation(
        session_root,
        session_id,
        pointer.generation,
        store_minor=pointer.store_minor,
        current_path=current_path,
        manifest_sha256=pointer.manifest_sha256,
        expected_snapshot_sequence=pointer.snapshot_sequence,
        chunk_cache=chunk_cache,
        report=report,
    )
    # An active load applies the whole journal, so a generation is only
    # uncoverable when a collection removed it mid-read — which the pointer
    # movement check in _load_active owns.
    if view is None:
        raise UnifiedStoreError("the active generation does not cover its own publication")
    return view


def _load_generation(  # noqa: PLR0912, PLR0915
    session_root: Path,
    session_id: str,
    generation: str,
    *,
    store_minor: int,
    current_path: Path,
    at_sequence: int | None = None,
    manifest_sha256: str | None = None,
    expected_snapshot_sequence: int | None = None,
    chunk_cache: _ChunkCache | None = None,
    report: dict[str, Any] | None = None,
) -> UnifiedStoreView | None:
    """Load one generation's publication; ``None`` when it cannot cover ``at_sequence``."""
    generation_dir = session_root / "generations" / generation
    _reject_symlink_components(session_root, generation_dir)
    manifest_value, manifest_body = _read_canonical_document(
        generation_dir / "manifest.json", "manifest.json"
    )
    if manifest_sha256 is not None and _sha256(manifest_body) != manifest_sha256:
        raise UnifiedStoreError("manifest digest mismatch")
    manifest = _validate_manifest(manifest_value)
    if manifest.session_id != session_id or manifest.generation != generation:
        raise UnifiedStoreError("the generation manifest disagrees with its selector")
    if (
        expected_snapshot_sequence is not None
        and manifest.snapshot_sequence != expected_snapshot_sequence
    ):
        raise UnifiedStoreError("CURRENT and manifest disagree")

    chunk_root = session_root / _CHUNKS_DIRNAME
    checkpoint = _read_referenced_document(generation_dir, manifest.checkpoint)
    if not isinstance(checkpoint, dict):
        raise UnifiedStoreError("the Core checkpoint must be an object")
    if checkpoint.get("checkpoint_version") != 1:
        raise UnifiedStoreError("Core checkpoint version mismatch")
    if manifest.checkpoint.chunks is not None:
        _reject_symlink_components(session_root, chunk_root)
        _attach_transcript(
            checkpoint,
            _CHECKPOINT_MESSAGES_PATH,
            _read_chunked_transcript(chunk_root, manifest.checkpoint.chunks, chunk_cache),
        )
    runtime_value = _read_referenced_document(generation_dir, manifest.runtime_state)
    projection_value = _read_referenced_document(generation_dir, manifest.projection_state)
    if manifest.projection_state.chunks is not None:
        _reject_symlink_components(session_root, chunk_root)
        _attach_transcript(
            projection_value,
            _PROJECTION_HISTORY_PATH,
            _read_chunked_transcript(chunk_root, manifest.projection_state.chunks, chunk_cache),
        )
    projection = _validate_projection_document(projection_value)
    runtime_session, runtime_sequence = _validate_runtime_document(runtime_value)
    if runtime_session != session_id:
        raise UnifiedStoreError("runtime state belongs to another session")
    if projection.session_id != session_id:
        raise UnifiedStoreError("projection state belongs to another session")
    if projection.snapshot["session"]["id"] != session_id:
        raise UnifiedStoreError("projection snapshot belongs to another session")
    if runtime_sequence != manifest.snapshot_sequence:
        raise UnifiedStoreError("runtime state sequence does not match manifest")
    if projection.snapshot_sequence != manifest.snapshot_sequence:
        raise UnifiedStoreError("projection state sequence does not match manifest")

    journal_path = session_root / manifest.journal_path
    _reject_symlink_components(session_root, journal_path)
    # Sample before reading so a concurrent append leaves a stale cache key.
    journal = _read_journal_detail(journal_path, manifest.first_sequence)
    records = journal.records
    if at_sequence is None:
        applied = records
    elif at_sequence == manifest.snapshot_sequence:
        applied = ()
    else:
        last_sequence = records[-1].sequence if records else manifest.snapshot_sequence
        if not manifest.first_sequence <= at_sequence <= last_sequence:
            return None
        applied = tuple(record for record in records if record.sequence <= at_sequence)
    watermark, snapshot = _replay_projection(projection.watermark, projection.snapshot, applied)
    sequence = applied[-1].sequence if applied else manifest.snapshot_sequence
    view = UnifiedStoreView(
        current=current_path,
        session_id=session_id,
        store_minor=store_minor,
        generation=generation,
        snapshot_sequence=manifest.snapshot_sequence,
        sequence=sequence,
        watermark=watermark,
        snapshot=snapshot,
        runtime_state=runtime_value,
        manifest_created_at=manifest.created_at,
        journal_fingerprint=tuple(record.record_sha256 for record in applied) or None,
    )
    if report is not None:
        report.update(
            manifest=manifest,
            runtime_value=runtime_value,
            journal_path=journal_path,
            journal_size=journal.stat_size,
            journal_mtime_ns=journal.stat_mtime_ns,
            journal_records=records,
            journal_complete_bytes=journal.complete_bytes,
            journal_last_start=journal.last_start,
        )
    return view


def _session_root(current: Path) -> Path:
    if current.name != "CURRENT":
        raise UnifiedStoreError(f"the unified store pointer must be the CURRENT file: {current}")
    session_root = current.parent
    if _SESSION_ID_PATTERN.fullmatch(session_root.name) is None:
        raise UnifiedStoreError(f"invalid session ID directory: {session_root.name!r}")
    _reject_symlink(session_root)
    return session_root


def _enumerate_generations(session_root: Path) -> list[str]:
    """Retained generation directory names, newest first."""
    generations = session_root / "generations"
    _reject_symlink(generations)
    try:
        names = [entry.name for entry in generations.iterdir()]
    except OSError:
        return []
    return sorted(
        (name for name in names if _GENERATION_PATTERN.fullmatch(name) is not None),
        reverse=True,
    )


def _read_current_bytes(session_root: Path) -> bytes | None:
    try:
        return (session_root / "CURRENT").read_bytes()
    except OSError:
        return None


def _as_store_error(failure: UnifiedStoreError | OSError) -> UnifiedStoreError:
    if isinstance(failure, UnifiedStoreError):
        return failure
    return UnifiedStoreError(f"the unified session store cannot be read: {failure}")


# --- Journal ------------------------------------------------------------------


def _read_journal_detail(path: Path, first_sequence: int) -> _JournalDetail:
    """Read a journal and locate its complete verified prefix."""
    _reject_symlink(path)
    with path.open("rb") as stream:
        stat = os.fstat(stream.fileno())
        data = stream.read(_MAX_DOCUMENT_BYTES + 1)
        if len(data) > _MAX_DOCUMENT_BYTES:
            raise UnifiedStoreError("recovery journal is too large")
    records, starts, complete = _parse_journal_tail(
        data, first_sequence=first_sequence, previous_digest=None
    )
    return _JournalDetail(
        records=records,
        complete_bytes=complete,
        last_start=starts[-1] if starts else 0,
        stat_size=stat.st_size,
        stat_mtime_ns=stat.st_mtime_ns,
    )


def _parse_journal_tail(
    data: bytes,
    *,
    first_sequence: int,
    previous_digest: str | None,
    pin: str | None = None,
) -> tuple[tuple[_JournalRecord, ...], tuple[int, ...], int]:
    """Validate a journal region, optionally pinning its first record."""
    lines = data.splitlines(keepends=True)
    records: list[_JournalRecord] = []
    starts: list[int] = []
    expected_sequence = first_sequence
    previous = previous_digest
    offset = 0
    for index, line in enumerate(lines):
        if not line.endswith(b"\n"):
            if index == len(lines) - 1:
                # A torn tail is the writer's last, unsynced record; the store
                # stands at the previous one until it is rewritten whole.
                break
            raise UnifiedStoreError("recovery journal has an unterminated interior record")
        raw = line[:-1]
        value = _decode_json(raw, f"recovery journal record {expected_sequence}")
        record = _validate_journal_record(value, expected_sequence)
        remainder = dict(value)
        stored_digest = remainder.pop("record_sha256")
        if stored_digest != _sha256_json(remainder):
            raise UnifiedStoreError(
                f"recovery journal record digest mismatch at {expected_sequence}"
            )
        if raw != _canonical_json(value):
            raise UnifiedStoreError("recovery journal record is not canonical JSON")
        if record.sequence != expected_sequence:
            raise UnifiedStoreError("recovery journal sequence gap")
        if record.previous_record_sha256 != previous:
            raise UnifiedStoreError("recovery journal digest chain mismatch")
        if pin is not None and not records and record.record_sha256 != pin:
            raise UnifiedStoreError("the recovery journal was rewritten underneath its reader")
        records.append(record)
        starts.append(offset)
        offset += len(line)
        expected_sequence += 1
        previous = record.record_sha256
    return tuple(records), tuple(starts), offset
