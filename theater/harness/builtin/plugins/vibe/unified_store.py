"""Bounded, read-only reader for Vibe unified session stores.

A Vibe session started with ``--experimental-harness`` persists itself under
``<save_dir>/unified/<session-id>/`` as a sequence of immutable *generations*
(each a snapshot plus a recovery journal) plus a ``CURRENT`` pointer naming the
active one. Theater's trajectory projection needs the effective conversation
projection of such a store without running, importing, or depending on Vibe.

This module re-implements the reading half of the store format
``mistral.vibe.unified-session-store/v1`` (minors 1–4) from its reference
semantics: pointer and manifest validation, RFC 8785 canonical documents and
journal digest chains, minor-4 transcript chunk pools, and journal replay of the
``projection_advanced`` / ``projection_delta`` records. It is deliberately
stricter than a permissive parser and looser than a full Vibe restore:

- Every store-structural document (CURRENT, manifest, projection envelope,
  journal record, delta operation) rejects unknown fields, exactly like the
  reference reader, so a store written by an ununderstood newer minor fails
  loudly rather than half-loading.
- The public session-state snapshot and the runtime state are validated only in
  the shape callers rely on (session protocol discriminants, history entries,
  session metadata). Their integrity is already pinned by document digests, and
  their full wire schemas belong to the session protocol, not to the store
  format, so additive protocol fields must not make a readable store fail.

Nothing here writes, mutates, or imports Vibe; every entry point is a pure
function of the bytes on disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import rfc8785

__all__ = [
    "STORE_FORMAT",
    "STORE_FORMAT_MINOR",
    "UnifiedStoreError",
    "UnifiedStoreReader",
    "UnifiedStoreRequiresNewer",
    "UnifiedStoreUpdate",
    "UnifiedStoreView",
    "current_fingerprint",
    "load_unified_store",
]


STORE_FORMAT = "mistral.vibe.unified-session-store/v1"
# The highest store-format minor this reader understands. Minor 2 introduced
# ``projection_delta`` journal records, minor 3 dropped the interop export
# document (now derived, and left unread here), and minor 4 moved conversation
# transcripts into the shared chunk pool. The field itself is optional and
# defaults to 1, so a pre-minor pointer restores as minor 1.
STORE_FORMAT_MINOR = 4

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

# An incremental reader retains parsed generation documents so polling an
# unchanged store costs one CURRENT read and one journal lstat instead of a
# full reload; the chunk cache gives those reloads (history paging, generation
# rollover) bounded reuse of the immutable, digest-named chunk bodies.
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

    ``snapshot`` is the effective public session state (checkpoint snapshot
    with every applied journal projection record folded in); ``sequence`` is the
    recovery sequence that state stands at — the generation's
    ``snapshot_sequence`` when no journal records apply, else the sequence of
    the last applied record. ``journal_fingerprint`` names the applied journal
    prefix by its digest chain, or ``None`` when nothing was applied.
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


def load_unified_store(
    current: Path,
    *,
    at_sequence: int | None = None,
    generation_hint: str | None = None,
    chunk_cache: _ChunkCache | None = None,
) -> UnifiedStoreView | None:
    """Read the unified session store whose pointer is ``current``.

    A normal load returns the active generation's effective projection. A
    historical load (``at_sequence`` given) returns the exact view standing at
    that recovery sequence — searching the hinted generation, then the active
    one, then retained generations newest-first — or ``None`` when retained
    data cannot cover the sequence. ``generation_hint`` without
    ``at_sequence`` pins the load to that generation's latest projection.

    A publication may race the read; an active load re-reads ``CURRENT`` and
    retries once when the pointer moved underneath it, so a view is never a
    mixture of two publications. ``chunk_cache`` lets a repeat load reuse the
    immutable, digest-verified chunk bodies it already read.
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
    """A cheap fingerprint of the CURRENT pointer, loading nothing else.

    Two calls that agree mean the pointer bytes are identical, nothing more —
    a caller deciding whether a full reload is warranted finds this cheaper
    than ``load_unified_store``. ``None`` when the pointer is missing,
    unreadable, or not a unified-store CURRENT path.
    """
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
    """Load the active publication plus the raw facts an incremental reader caches.

    The second element is the load report: the validated manifest, the runtime
    document, the journal path with the ``(size, mtime_ns)`` sampled before the
    journal was read, the applied records, and the byte extent of the complete
    (chain-verified, fully terminated) journal prefix. A torn tail beyond that
    extent is not part of the store yet and is left for the next consume.
    """
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

    Only a moved pointer can mean a collection took the active generation out
    from under the read; anything else is a genuinely broken store, so the
    original failure stands. The retry fills a fresh report: the first attempt
    may have written half of one before it failed.
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

    The newer-minor check runs before strict validation: a newer writer may
    have added pointer fields alongside the minor bump, and rejecting those
    first would hide the actionable cause. A ``report`` sink receives the exact
    pointer bytes, so an incremental reader can compare bytes before paying for
    a re-parse.
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
    """Load one generation's publication; ``None`` when it cannot cover ``at_sequence``.

    Every document the manifest names is read and digest-checked, the journal
    is chain-verified as a whole, and only then are the projection records up
    to ``at_sequence`` folded into the snapshot.
    """
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
    # Sample the journal before reading it: a concurrent append then makes the
    # sampled key look stale and forces the next incremental consume, whereas
    # sampling afterwards could record a size that already covers records this
    # load never saw.
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


# --- Canonical JSON and digests ------------------------------------------------


def _canonical_json(value: Any) -> bytes:
    """The RFC 8785 encoding of ``value``, matching the reference writer.

    The standard encoder agrees with RFC 8785 byte for byte except at floats,
    integers outside the safe domain, and non-ASCII object keys, where this
    falls back to ``rfc8785``. A value none of them can encode — an unsafe
    integer, a lone surrogate — has no canonical form, so it cannot have been
    what the writer digested.
    """
    if _standard_encoder_is_canonical(value):
        try:
            return json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        except (RecursionError, UnicodeEncodeError):
            pass
    try:
        return rfc8785.dumps(value)
    except Exception as exc:
        raise UnifiedStoreError(f"a stored value has no canonical JSON encoding: {exc}") from exc


def _standard_encoder_is_canonical(value: Any) -> bool:
    pending: list[Any] = [value]
    while pending:
        item = pending.pop()
        item_type = type(item)
        if item_type is str or item is None or item_type is bool:
            continue
        if item_type is dict:
            for key, nested in item.items():
                if type(key) is not str or not key.isascii():
                    return False
                pending.append(nested)
            continue
        if item_type is list:
            pending.extend(item)
            continue
        if item_type is int:
            if item not in _CANONICAL_INTEGER_RANGE:
                return False
            continue
        return False
    return True


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256(_canonical_json(value))


# --- Document reads -----------------------------------------------------------


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise UnifiedStoreError(f"stored path cannot be a symbolic link: {path}")


def _reject_symlink_components(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise UnifiedStoreError(f"stored path escapes its configured root: {path}") from exc
    current = root
    _reject_symlink(current)
    for part in relative.parts:
        current = current / part
        _reject_symlink(current)


def _read_document_body(path: Path, description: str) -> bytes:
    _reject_symlink(path)
    with path.open("rb") as stream:
        data = stream.read(_MAX_DOCUMENT_BYTES + 1)
        if len(data) > _MAX_DOCUMENT_BYTES:
            raise UnifiedStoreError(f"stored JSON document is too large: {description}")
    if not data.endswith(b"\n"):
        raise UnifiedStoreError(f"stored JSON document is not newline terminated: {description}")
    return data[:-1]


def _decode_json(body: bytes, description: str) -> Any:
    try:
        return json.loads(body)
    except (RecursionError, ValueError) as exc:
        raise UnifiedStoreError(f"stored JSON document is not valid JSON: {description}") from exc


def _decode_canonical_document(body: bytes, description: str) -> Any:
    value = _decode_json(body, description)
    if body != _canonical_json(value):
        raise UnifiedStoreError(f"stored JSON document is not canonical JSON: {description}")
    return value


def _read_canonical_document(path: Path, description: str) -> tuple[Any, bytes]:
    body = _read_document_body(path, description)
    return _decode_canonical_document(body, description), body


def _read_referenced_document(generation_dir: Path, descriptor: _StoredFile) -> Any:
    body = _read_document_body(generation_dir / descriptor.path, descriptor.path)
    if _sha256(body) != descriptor.sha256:
        raise UnifiedStoreError(f"stored record digest mismatch: {descriptor.path}")
    return _decode_json(body, descriptor.path)


class _ChunkCache:
    """Bounded LRU of chunk bodies, keyed by the content digest that names them.

    A chunk is immutable once written and its file name is its digest, so a
    cached body is exactly what a fresh read would return (the read path
    verifies the digest before a body may enter the cache). Oversized bodies
    are returned uncached, keeping the resident set under the configured
    budget; least-recently-used digests are evicted first.
    """

    def __init__(self, budget: int = _READER_CHUNK_CACHE_BYTES) -> None:
        if budget <= 0:
            raise UnifiedStoreError("chunk cache budget must be positive")
        self._budget = budget
        self._bodies: OrderedDict[str, bytes] = OrderedDict()
        self._resident = 0

    def read(self, chunk_root: Path, digest: str) -> bytes:
        cached = self._bodies.get(digest)
        if cached is not None:
            self._bodies.move_to_end(digest)
            return cached
        path = chunk_root / f"{digest}.json"
        _reject_symlink(path)
        body = _read_document_body(path, f"chunk {digest}")
        if _sha256(body) != digest:
            raise UnifiedStoreError(f"stored chunk digest mismatch: {digest}")
        if len(body) <= self._budget:
            self._bodies[digest] = body
            self._resident += len(body)
            while self._resident > self._budget:
                self._resident -= len(self._bodies.popitem(last=False)[1])
        return body

    def clear(self) -> None:
        self._bodies.clear()
        self._resident = 0


def _read_chunked_transcript(
    chunk_root: Path, digests: tuple[str, ...], chunk_cache: _ChunkCache | None = None
) -> list[Any]:
    items: list[Any] = []
    total_bytes = 0
    for digest in digests:
        if chunk_cache is not None:
            body = chunk_cache.read(chunk_root, digest)
        else:
            path = chunk_root / f"{digest}.json"
            _reject_symlink(path)
            body = _read_document_body(path, path.name)
            if _sha256(body) != digest:
                raise UnifiedStoreError(f"stored chunk digest mismatch: {digest}")
        total_bytes += len(body)
        if total_bytes > _MAX_DOCUMENT_BYTES:
            raise UnifiedStoreError("stored chunked transcript is too large")
        chunk = _decode_json(body, f"chunk {digest}")
        if not isinstance(chunk, list):
            raise UnifiedStoreError(f"stored chunk is not a transcript run: {digest}")
        items.extend(chunk)
    return items


def _attach_transcript(document: dict[str, Any], path: tuple[str, ...], items: list[Any]) -> None:
    node: Any = document
    for key in path[:-1]:
        if not isinstance(node, dict):
            raise UnifiedStoreError("chunked document is missing its transcript container")
        node = node.get(key)
    if not isinstance(node, dict) or node.get(path[-1]) != []:
        raise UnifiedStoreError("chunked document envelope must hold an empty transcript")
    node[path[-1]] = items


# --- Journal ------------------------------------------------------------------


def _read_journal(path: Path, first_sequence: int) -> tuple[_JournalRecord, ...]:
    return _read_journal_detail(path, first_sequence).records


def _read_journal_detail(path: Path, first_sequence: int) -> _JournalDetail:
    """Read the whole journal, reporting where its complete prefix ends.

    ``complete_bytes`` bounds the fully terminated, chain-verified records — a
    torn tail beyond it is the writer's last, unsynced record and is not part
    of the store yet — and ``last_start`` is the byte offset at which the last
    complete record begins, so an incremental consume can re-pin it. The stat
    is sampled before the file is read, so a concurrent append makes the
    sampled key look stale instead of covering records this read never saw.
    """
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
    """Validate a journal region exactly as a whole read would.

    ``first_sequence`` and ``previous_digest`` describe the record the first
    complete line must continue: sequence continuity and the digest chain are
    checked per record, and each record's own digest and canonical form are
    re-proven from its bytes. ``pin`` — the digest of a record an earlier read
    already verified at the region's start — must match that line again,
    re-proving the boundary an incremental consume trusts before new records
    are accepted against it. Returns the parsed records, the byte offset each
    one starts at within ``data``, and the length of the complete-record
    prefix; a torn final line is excluded from all three.
    """
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

    A newer writer may add pointer fields alongside the minor bump; strict
    validation would reject those and hide the cause, so the minor is read from
    the raw document first. A pointer at this reader's minor still passes
    through strict validation, so an unexpected field there stays a broken
    store.
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
    """Validate the runtime-state metadata callers rely on.

    The runtime state holds the Runtime's private recovery model, which this
    reader neither replays nor interprets; only the identity and session
    metadata Theater reads are checked, and the document is otherwise passed
    through verbatim.
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

    Unknown keys inside the state are tolerated: the document digest already
    pins every byte of it, and the session protocol — not the store format —
    owns this schema, so an additive protocol field must not make a readable
    store unreadable. The keys Theater reads are required and type-checked.
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


# --- Incremental reader --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnifiedStoreUpdate:
    """One active load plus what it changed relative to a baseline view.

    ``changed_entry_ids`` names the raw entry ids the applied journal records
    may have touched, relative to ``baseline`` — the view the reader held
    before this load. Entries it does not name share their object with the
    baseline, so a caller that trusts the baseline can skip re-fingerprinting
    them. ``None`` means the change set is unknown (a full reload, or a record
    that rebuilds the history wholesale) and only a full diff is correct. An
    empty set means nothing in the history moved at all.
    """

    view: UnifiedStoreView
    changed_entry_ids: frozenset[str] | None
    baseline: UnifiedStoreView | None


@dataclass(slots=True)
class _ReaderState:
    """Everything the incremental fast path needs for one session's store.

    ``journal_verified`` is the byte extent of the complete, chain-verified
    journal prefix; ``last_record_start`` is where its last record begins, and
    the two digests pin that record from both sides. A torn tail beyond the
    verified extent is not part of the store and is consumed only once it is
    rewritten whole.
    """

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
    """Bounded incremental reader state for one unified session store.

    An unchanged store is three cheap facts — the CURRENT bytes, the journal's
    ``lstat``, and the digest chain — so polling through this reader consumes
    only appended journal bytes and reuses the parsed, validated generation
    documents instead of re-reading and re-checking the whole store every
    tick. ``load`` fails closed: any anomaly (a moved pointer, a truncated or
    in-place-rewritten journal, a chain break, a parse error) falls back to a
    full ``load_unified_store``, which stays the authority and re-establishes
    the cached state. The trust model matches the store's writer: the
    verified journal prefix is immutable and only the torn tail may be
    rewritten, and the consume path re-pins the last verified record's digest
    before accepting new records against it.

    The reader holds exactly one session's state; a load for a different
    session discards it. The chunk cache is bounded (``chunk_cache_bytes``)
    and shared with plain ``load_unified_store`` calls, so full reloads and
    history paging reuse a generation's immutable chunks instead of re-reading
    the same bodies per load.
    """

    def __init__(self, *, chunk_cache_bytes: int = _READER_CHUNK_CACHE_BYTES) -> None:
        self._chunks = _ChunkCache(chunk_cache_bytes)
        self._state: _ReaderState | None = None

    @property
    def chunk_cache(self) -> _ChunkCache:
        """The bounded chunk-body cache to thread through plain store loads."""
        return self._chunks

    @property
    def last_view(self) -> UnifiedStoreView | None:
        """The view this reader last loaded, whatever later polls did with it."""
        return None if self._state is None else self._state.view

    def reset(self) -> None:
        """Drop the cached state and chunk bodies; the next load starts cold."""
        self._state = None
        self._chunks.clear()

    def load(self, current: Path) -> UnifiedStoreUpdate:
        """Load the active publication, incrementally when the cache allows it."""
        current_path = Path(current)
        session_root = _session_root(current_path)
        session_id = session_root.name
        state = self._state
        if state is not None and (
            state.session_root != session_root or state.session_id != session_id
        ):
            # Cached state is only ever valid for one session's store.
            self.reset()
            state = None
        if state is None:
            return self._reload(session_root, session_id, current_path)
        data = _read_current_bytes(session_root)
        if data is None or data != state.current_bytes:
            return self._reload(session_root, session_id, current_path)
        try:
            stat = os.lstat(state.journal_path)
        except OSError:
            return self._reload(session_root, session_id, current_path)
        # A hit skips the checks a full load runs, so re-check the links a
        # full load would have rejected before trusting the cached documents.
        _reject_symlink(state.session_root)
        _reject_symlink(state.session_root / "generations")
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
        """Read the journal from its last verified record to the end.

        Returns the parsed records (the pinned boundary record included when
        one exists), the offset each starts at within the region, the length
        of the complete prefix, and the region's start offset. Re-parsing the
        first line against its pinned digest re-proves that the boundary the
        fast path trusts still ends where the reader left it.
        """
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
        """Fold new records in, naming what they may have changed by construction.

        ``append_entry``/``replace_entry``/``remove_entry`` name the raw entry
        ids they touch and leave every other entry object shared with the
        baseline, so the caller may skip those fingerprints;
        ``set_history_entries`` and ``projection_advanced`` rebuild the
        history, so the change set is reported as unknown and the caller falls
        back to a full diff.
        """
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
