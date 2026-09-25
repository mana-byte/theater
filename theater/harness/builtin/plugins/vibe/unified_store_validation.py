"""Schema and journal validation for the Vibe unified-session store."""

from __future__ import annotations

from typing import Any

from .unified_store_types import (
    _GENERATION_PATTERN,
    _JOURNAL_PATH_PATTERN,
    _JOURNAL_RECORD_TYPES,
    _MAX_SAFE_JSON_INTEGER,
    _SESSION_ID_PATTERN,
    _SESSION_STATUS_TYPES,
    _SHA256_PATTERN,
    _TIMESTAMP_PATTERN,
    _TURN_STATUSES,
    STORE_FORMAT,
    STORE_FORMAT_MINOR,
    UnifiedStoreError,
    _CurrentPointer,
    _JournalRecord,
    _Manifest,
    _ProjectionDocument,
    _StoredFile,
)


def _validate_journal_record(value: Any, expected_sequence: int) -> _JournalRecord:
    allowed = {
        "recovery_journal_record_version",
        "sequence",
        "previous_record_sha256",
        "record_sha256",
        "type",
        "payload",
    }
    _require_strict_object(
        value,
        allowed,
        f"recovery journal record {expected_sequence}",
        required=allowed,
    )
    assert isinstance(value, dict)
    _literal_integer(value["recovery_journal_record_version"], 1, "recovery journal record version")
    sequence = _safe_integer(value["sequence"], "recovery journal record sequence", minimum=1)
    previous = value["previous_record_sha256"]
    if previous is not None and _SHA256_PATTERN.fullmatch(previous) is None:
        raise UnifiedStoreError("recovery journal previous-record digest is malformed")
    digest = value["record_sha256"]
    if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
        raise UnifiedStoreError("recovery journal record digest is malformed")
    record_type = value["type"]
    if not isinstance(record_type, str) or record_type not in _JOURNAL_RECORD_TYPES:
        raise UnifiedStoreError(f"unknown recovery journal record type: {record_type!r}")
    payload = value["payload"]
    if not isinstance(payload, dict):
        raise UnifiedStoreError(f"recovery journal record {sequence} payload must be an object")
    watermark: int | None = None
    snapshot: dict[str, Any] | None = None
    delta: tuple[dict[str, Any], ...] | None = None
    if record_type == "projection_advanced":
        _require_strict_object(payload, {"watermark", "snapshot"}, "projection_advanced payload")
        watermark = _safe_integer(payload["watermark"], "projection watermark")
        validated = _validate_public_session_state(payload["snapshot"], "projection snapshot")
        snapshot = validated
    elif record_type == "projection_delta":
        _require_strict_object(payload, {"watermark", "delta"}, "projection_delta payload")
        watermark = _safe_integer(payload["watermark"], "projection watermark")
        delta = _validate_projection_delta(payload["delta"])
    return _JournalRecord(
        sequence=sequence,
        record_sha256=digest,
        record_type=record_type,
        previous_record_sha256=previous,
        watermark=watermark,
        snapshot=snapshot,
        delta=delta,
    )


def _replay_projection(
    watermark: int, snapshot: dict[str, Any], records: tuple[_JournalRecord, ...]
) -> tuple[int, dict[str, Any]]:
    for record in records:
        # Non-projection records carry no public state; they only advance the
        # recovery sequence, which the caller reports from the record list.
        # Validation sets ``watermark`` exactly on the projection types.
        if record.watermark is None:
            continue
        if record.record_type == "projection_advanced":
            if record.watermark < watermark:
                raise UnifiedStoreError("projection watermark moved backwards")
            watermark = record.watermark
            snapshot = record.snapshot or {}
        elif record.record_type == "projection_delta":
            if record.watermark < watermark:
                raise UnifiedStoreError("projection watermark moved backwards")
            watermark = record.watermark
            snapshot = _apply_projection_delta(snapshot, record.delta or ())
    return watermark, snapshot


def _validate_projection_delta(value: Any) -> tuple[dict[str, Any], ...]:  # noqa: PLR0912
    if not isinstance(value, list):
        raise UnifiedStoreError("projection delta must be a list of operations")
    ops: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise UnifiedStoreError("projection delta operation must be an object")
        kind = item.get("op")
        if kind == "append_entry":
            _require_strict_object(item, {"op", "entry"}, "append_entry operation")
            if not isinstance(item["entry"], dict):
                raise UnifiedStoreError("append_entry entry must be an object")
        elif kind == "replace_entry":
            _require_strict_object(item, {"op", "id", "entry"}, "replace_entry operation")
            if not isinstance(item["id"], str):
                raise UnifiedStoreError("replace_entry id must be a string")
            if not isinstance(item["entry"], dict):
                raise UnifiedStoreError("replace_entry entry must be an object")
        elif kind == "remove_entry":
            _require_strict_object(item, {"op", "id"}, "remove_entry operation")
            if not isinstance(item["id"], str):
                raise UnifiedStoreError("remove_entry id must be a string")
        elif kind == "set_history_entries":
            _require_strict_object(item, {"op", "entries"}, "set_history_entries operation")
            if not isinstance(item["entries"], list) or not all(
                isinstance(entry, dict) for entry in item["entries"]
            ):
                raise UnifiedStoreError("set_history_entries entries must be a list of objects")
        elif kind == "set_envelope":
            _require_strict_object(item, {"op", "state"}, "set_envelope operation")
            _validate_public_session_state(item["state"], "projection envelope")
        else:
            raise UnifiedStoreError(f"unknown projection delta operation: {kind!r}")
        ops.append(item)
    return tuple(ops)


def _apply_projection_delta(
    snapshot: dict[str, Any], delta: tuple[dict[str, Any], ...]
) -> dict[str, Any]:
    entries = list(snapshot["history"]["entries"])
    envelope = snapshot
    for op in delta:
        kind = op["op"]
        if kind == "append_entry":
            entries.append(op["entry"])
        elif kind == "replace_entry":
            entry_id = op["id"]
            for index, entry in enumerate(entries):
                if entry.get("id") == entry_id:
                    entries[index] = op["entry"]
                    break
            else:
                raise UnifiedStoreError("projection delta replaces an absent entry")
        elif kind == "remove_entry":
            entry_id = op["id"]
            remaining = [entry for entry in entries if entry.get("id") != entry_id]
            if len(remaining) == len(entries):
                raise UnifiedStoreError("projection delta removes an absent entry")
            entries = remaining
        elif kind == "set_history_entries":
            entries = list(op["entries"])
        elif kind == "set_envelope":
            envelope = op["state"]
    history = envelope.get("history")
    if not isinstance(history, dict):
        raise UnifiedStoreError("projection envelope has no history container")
    result = dict(envelope)
    result["history"] = {**history, "entries": entries}
    return result


# --- Document validation ------------------------------------------------------


def _newer_minor(value: Any) -> int | None:
    """The pointer's minor when it is newer than this reader understands.

    Read from the raw document so strict validation cannot hide the newer-minor cause.
    """
    if not isinstance(value, dict):
        return None
    minor = value.get("store_format_minor")
    if isinstance(minor, bool) or not isinstance(minor, int):
        return None
    return minor if minor > STORE_FORMAT_MINOR else None


def _validate_current(value: Any) -> _CurrentPointer:
    allowed = {
        "store_format",
        "store_format_minor",
        "session_id",
        "generation",
        "snapshot_sequence",
        "manifest_sha256",
    }
    required = allowed - {"store_format", "store_format_minor"}
    _require_strict_object(value, allowed, "CURRENT", required=required)
    assert isinstance(value, dict)
    if value.get("store_format", STORE_FORMAT) != STORE_FORMAT:
        raise UnifiedStoreError(
            f"not a unified session store pointer: {value.get('store_format')!r}"
        )
    minor = value.get("store_format_minor", 1)
    if (
        isinstance(minor, bool)
        or not isinstance(minor, int)
        or not 1 <= minor <= STORE_FORMAT_MINOR
    ):
        raise UnifiedStoreError(f"unsupported store_format_minor: {minor!r}")
    session_id = value["session_id"]
    if not isinstance(session_id, str) or _SESSION_ID_PATTERN.fullmatch(session_id) is None:
        raise UnifiedStoreError(f"invalid session ID: {session_id!r}")
    generation = value["generation"]
    if not isinstance(generation, str) or _GENERATION_PATTERN.fullmatch(generation) is None:
        raise UnifiedStoreError(f"invalid generation: {generation!r}")
    snapshot_sequence = _safe_integer(value["snapshot_sequence"], "CURRENT snapshot_sequence")
    manifest_sha256 = value["manifest_sha256"]
    if not isinstance(manifest_sha256, str) or _SHA256_PATTERN.fullmatch(manifest_sha256) is None:
        raise UnifiedStoreError("CURRENT manifest digest is malformed")
    return _CurrentPointer(
        session_id=session_id,
        store_minor=minor,
        generation=generation,
        snapshot_sequence=snapshot_sequence,
        manifest_sha256=manifest_sha256,
    )


def _validate_stored_file(
    value: Any, description: str, *, extra: set[str] | frozenset[str] = frozenset()
) -> _StoredFile:
    allowed = {"path", "sha256", "chunks"} | set(extra)
    _require_strict_object(value, allowed, description, required={"path", "sha256"})
    assert isinstance(value, dict)
    path = value["path"]
    if not isinstance(path, str) or path in {"", ".", ".."} or "/" in path or "\\" in path:
        raise UnifiedStoreError(f"{description} path must be one file name")
    sha256 = value["sha256"]
    if not isinstance(sha256, str) or _SHA256_PATTERN.fullmatch(sha256) is None:
        raise UnifiedStoreError(f"{description} digest is malformed")
    raw_chunks = value.get("chunks")
    chunks: tuple[str, ...] | None
    if raw_chunks is None:
        chunks = None
    else:
        if not isinstance(raw_chunks, list):
            raise UnifiedStoreError(f"{description} chunk list is malformed")
        for chunk in raw_chunks:
            if not isinstance(chunk, str) or _SHA256_PATTERN.fullmatch(chunk) is None:
                raise UnifiedStoreError(f"{description} names a malformed chunk digest")
        chunks = tuple(raw_chunks)
    return _StoredFile(path=path, sha256=sha256, chunks=chunks)


def _validate_manifest(value: Any) -> _Manifest:
    allowed = {
        "manifest_version",
        "session_id",
        "generation",
        "created_at",
        "snapshot_sequence",
        "execution_state",
        "checkpoint",
        "runtime_state",
        "projection_state",
        "interop_export",
        "recovery_journal_segment",
    }
    required = allowed - {"interop_export"}
    _require_strict_object(value, allowed, "generation manifest", required=required)
    assert isinstance(value, dict)
    _literal_integer(value["manifest_version"], 1, "manifest version")
    session_id = value["session_id"]
    if not isinstance(session_id, str):
        raise UnifiedStoreError("manifest session ID must be a string")
    generation = value["generation"]
    if not isinstance(generation, str) or _GENERATION_PATTERN.fullmatch(generation) is None:
        raise UnifiedStoreError(f"invalid generation: {generation!r}")
    created_at = value["created_at"]
    if not isinstance(created_at, str) or _TIMESTAMP_PATTERN.fullmatch(created_at) is None:
        raise UnifiedStoreError(f"invalid manifest timestamp: {created_at!r}")
    snapshot_sequence = _safe_integer(value["snapshot_sequence"], "manifest snapshot_sequence")
    execution_state = value["execution_state"]
    if execution_state not in {"quiescent", "recoverable"}:
        raise UnifiedStoreError(f"invalid execution state: {execution_state!r}")
    checkpoint = _validate_stored_file(
        value["checkpoint"], "checkpoint record", extra=frozenset({"checkpoint_version"})
    )
    _literal_integer(
        value["checkpoint"].get("checkpoint_version", 1), 1, "checkpoint record version"
    )
    runtime_state = _validate_stored_file(value["runtime_state"], "runtime state record")
    if runtime_state.chunks is not None:
        raise UnifiedStoreError("the runtime state carries no transcript to pool")
    projection_state = _validate_stored_file(value["projection_state"], "projection state record")
    interop_export = None
    if value.get("interop_export") is not None:
        # Only a generation written before minor 3 names an export document; it
        # is derived from the checkpoint now, so the record is validated and
        # then left unread.
        interop_export = _validate_stored_file(value["interop_export"], "interop export record")
        if interop_export.chunks is not None:
            raise UnifiedStoreError("an interop export record predates the chunk pool")
    segment = value["recovery_journal_segment"]
    _require_strict_object(segment, {"path", "first_sequence"}, "recovery journal segment")
    assert isinstance(segment, dict)
    journal_path = segment["path"]
    if not isinstance(journal_path, str) or _JOURNAL_PATH_PATTERN.fullmatch(journal_path) is None:
        raise UnifiedStoreError("invalid recovery journal segment path")
    first_sequence = _safe_integer(
        segment["first_sequence"], "recovery journal first sequence", minimum=1
    )
    if first_sequence != snapshot_sequence + 1:
        raise UnifiedStoreError("journal segment must begin after the snapshot sequence")
    if execution_state == "recoverable" and interop_export is not None:
        raise UnifiedStoreError("a recoverable generation cannot have an interop export")
    return _Manifest(
        session_id=session_id,
        generation=generation,
        created_at=created_at,
        snapshot_sequence=snapshot_sequence,
        execution_state=execution_state,
        checkpoint=checkpoint,
        runtime_state=runtime_state,
        projection_state=projection_state,
        interop_export=interop_export,
        journal_path=journal_path,
        first_sequence=first_sequence,
    )


def _validate_projection_document(value: Any) -> _ProjectionDocument:
    """Validate a projection-state document and return its validated facts."""
    _require_strict_object(
        value,
        {"projection_state_version", "session_id", "snapshot_sequence", "watermark", "snapshot"},
        "projection state",
    )
    assert isinstance(value, dict)
    _literal_integer(value["projection_state_version"], 1, "projection state version")
    session_id = value["session_id"]
    if not isinstance(session_id, str):
        raise UnifiedStoreError("projection state session ID must be a string")
    snapshot_sequence = _safe_integer(value["snapshot_sequence"], "projection state sequence")
    watermark = _safe_integer(value["watermark"], "projection state watermark")
    snapshot = _validate_public_session_state(value["snapshot"], "projection snapshot")
    return _ProjectionDocument(
        session_id=session_id,
        snapshot_sequence=snapshot_sequence,
        watermark=watermark,
        snapshot=snapshot,
    )


def _validate_runtime_document(value: Any) -> tuple[str, int]:
    """Validate only the runtime-state metadata Theater reads; the private recovery model passes
    through.
    """
    if not isinstance(value, dict):
        raise UnifiedStoreError("runtime state must be an object")
    version = value.get("runtime_state_version", 3)
    if isinstance(version, bool) or version != 3:
        raise UnifiedStoreError(f"unsupported Runtime state version: {version!r}")
    session_id = value.get("session_id")
    if not isinstance(session_id, str):
        raise UnifiedStoreError("runtime state session ID must be a string")
    snapshot_sequence = _safe_integer(value.get("snapshot_sequence"), "runtime state sequence")
    metadata = value.get("session_metadata")
    if not isinstance(metadata, dict):
        raise UnifiedStoreError("runtime state has no session metadata")
    root_session_id = metadata.get("root_session_id")
    if not isinstance(root_session_id, str):
        raise UnifiedStoreError("runtime session metadata has no root session ID")
    cwd = metadata.get("cwd")
    if not isinstance(cwd, str) and cwd is not None:
        raise UnifiedStoreError("runtime session metadata has a malformed cwd")
    for key in ("active_model", "agent_name"):
        pin = metadata.get(key)
        if not isinstance(pin, str) and pin is not None:
            raise UnifiedStoreError(f"runtime session metadata has a malformed {key}")
    return session_id, snapshot_sequence


def _validate_public_session_state(value: Any, description: str) -> dict[str, Any]:  # noqa: PLR0912
    """Check the public session-state shape in the bounded way described above.

    Unknown keys are tolerated: digests pin the bytes and the session protocol owns the schema.
    """
    if not isinstance(value, dict):
        raise UnifiedStoreError(f"{description} must be an object")
    if value.get("format") != "harness.public-session-state/v1":
        raise UnifiedStoreError(f"{description} has an unsupported format: {value.get('format')!r}")
    session = value.get("session")
    if not isinstance(session, dict):
        raise UnifiedStoreError(f"{description} has no session object")
    if not isinstance(session.get("id"), str):
        raise UnifiedStoreError(f"{description} session has no ID")
    status = session.get("status")
    if not isinstance(status, dict) or status.get("type") not in _SESSION_STATUS_TYPES:
        raise UnifiedStoreError(f"{description} session has an invalid status")
    _safe_integer(session.get("createdAt"), f"{description} session createdAt")
    _safe_integer(session.get("updatedAt"), f"{description} session updatedAt")
    history = value.get("history")
    if not isinstance(history, dict):
        raise UnifiedStoreError(f"{description} has no history page")
    if "range" in history and history["range"] != "latest":
        raise UnifiedStoreError(f"{description} history page must be the latest range")
    entries = history.get("entries")
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise UnifiedStoreError(f"{description} history entries must be a list of objects")
    if "cursor" in history and not isinstance(history["cursor"], dict):
        raise UnifiedStoreError(f"{description} history cursor must be an object")
    turn_queue = value.get("turnQueue")
    if not isinstance(turn_queue, dict):
        raise UnifiedStoreError(f"{description} has no turn queue")
    if not isinstance(turn_queue.get("items"), list):
        raise UnifiedStoreError(f"{description} turn queue items must be a list")
    if not isinstance(turn_queue.get("paused"), bool):
        raise UnifiedStoreError(f"{description} turn queue paused flag must be a boolean")
    _safe_integer(turn_queue.get("maxItems"), f"{description} turn queue capacity", minimum=1)
    active_callbacks = value.get("activeCallbacks")
    if not isinstance(active_callbacks, list):
        raise UnifiedStoreError(f"{description} active callbacks must be a list")
    latest_turn = value.get("latestTurn")
    if latest_turn is not None and (
        not isinstance(latest_turn, dict) or latest_turn.get("status") not in _TURN_STATUSES
    ):
        raise UnifiedStoreError(f"{description} latest turn has an invalid status")
    return value


# --- Validation primitives ----------------------------------------------------


def _require_strict_object(
    value: Any, allowed: set[str], description: str, *, required: set[str] | None = None
) -> None:
    if not isinstance(value, dict):
        raise UnifiedStoreError(f"{description} must be an object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise UnifiedStoreError(f"{description} has unknown fields: {', '.join(unknown)}")
    missing = sorted((required if required is not None else allowed) - set(value))
    if missing:
        raise UnifiedStoreError(f"{description} is missing fields: {', '.join(missing)}")


def _safe_integer(value: Any, description: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnifiedStoreError(f"{description} must be an integer")
    if not -_MAX_SAFE_JSON_INTEGER <= value <= _MAX_SAFE_JSON_INTEGER:
        raise UnifiedStoreError(f"{description} is outside the safe JSON integer range")
    if value < minimum:
        raise UnifiedStoreError(f"{description} must be at least {minimum}")
    return value


def _literal_integer(value: Any, expected: int, description: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value != expected:
        raise UnifiedStoreError(f"{description} must be {expected}")
