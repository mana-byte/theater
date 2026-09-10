"""Mutable source for Vibe's experimental Unified Session Store."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from theater.constants.trajectory import TRAJECTORY_PAGE_RECORD_LIMIT
from theater.harness.contracts.events import Event, EventKind, TokenUsage, clip
from theater.harness.contracts.source import (
    Attachment,
    Batch,
    History,
    HistoryPage,
    ReceiptAdmission,
    Source,
    StreamPoint,
)
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.models import Status
from theater.provenance import TranscriptProvenance, is_trusted_provenance, normalize_provenance
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
    same_location,
    trusted_location_unavailable_reason,
)

from .trajectory import session_usage_fact
from .unified_projection import (
    entry_fingerprint,
    entry_identity,
    logical_stream_id,
    project_unified_entry,
)
from .unified_store import (
    UnifiedStoreError,
    UnifiedStoreReader,
    UnifiedStoreUpdate,
    UnifiedStoreView,
    load_unified_store,
)

if TYPE_CHECKING:
    from .observer import VibeObserver

_CHECKPOINT_MAX_BYTES = 4096
_TERMINAL_TURNS = frozenset({"completed", "failed", "interrupted"})


def _nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _checkpoint(raw: str | None) -> dict | None:
    if raw is None or len(raw.encode("utf-8", errors="ignore")) > _CHECKPOINT_MAX_BYTES:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("version") != 1:
        return None
    if value.get("backend") != "vibe-unified":
        return None
    required_strings = ("location", "session_id", "generation")
    if any(
        not isinstance(value.get(name), str) or not value.get(name) for name in required_strings
    ):
        return None
    if any(
        _nonnegative_int(value.get(name)) is None
        for name in ("sequence", "watermark", "snapshot_sequence")
    ):
        return None
    usage = value.get("usage")
    if not isinstance(usage, list) or len(usage) != 3:
        return None
    if any(_nonnegative_int(item) is None for item in usage):
        return None
    return value


def _entries(view: UnifiedStoreView) -> list[dict]:
    history = view.snapshot.get("history")
    values = history.get("entries") if isinstance(history, dict) else None
    return (
        [entry for entry in values if isinstance(entry, dict)] if isinstance(values, list) else []
    )


def _runtime_metadata(view: UnifiedStoreView) -> dict:
    value = view.runtime_state.get("session_metadata")
    return value if isinstance(value, dict) else {}


def _session(view: UnifiedStoreView) -> dict:
    value = view.snapshot.get("session")
    return value if isinstance(value, dict) else {}


def _latest_turn(view: UnifiedStoreView) -> dict:
    value = view.snapshot.get("latestTurn")
    return value if isinstance(value, dict) else {}


def _usage(view: UnifiedStoreView) -> tuple[int, int, int]:
    raw = _session(view).get("tokenUsage")
    if not isinstance(raw, dict):
        return 0, 0, 0
    prompt = _nonnegative_int(raw.get("inputTokens")) or 0
    completion = _nonnegative_int(raw.get("outputTokens")) or 0
    cached = min(_nonnegative_int(raw.get("cachedInputTokens")) or 0, prompt)
    return prompt, completion, cached


def _active_model(view: UnifiedStoreView) -> str | None:
    model = _runtime_metadata(view).get("active_model")
    return model if isinstance(model, str) and model else None


def _has_durable_usage(view: UnifiedStoreView) -> bool:
    return isinstance(_session(view).get("tokenUsage"), dict)


def _source_status(view: UnifiedStoreView) -> Status | None:
    value = _session(view).get("status")
    status_type = value.get("type") if isinstance(value, dict) else None
    if not isinstance(status_type, str):
        return None
    return {
        "running": Status.WORKING,
        "blocked": Status.AWAITING_INPUT,
        "idle": Status.IDLE,
        "failed": Status.IDLE,
        "archived": Status.IDLE,
    }.get(status_type)


def _turn_marker(view: UnifiedStoreView) -> tuple[str | None, str | None]:
    turn = _latest_turn(view)
    turn_id = turn.get("id")
    status = turn.get("status")
    return (
        turn_id if isinstance(turn_id, str) and turn_id else None,
        status if isinstance(status, str) and status else None,
    )


def _encode_checkpoint(view: UnifiedStoreView) -> str:
    turn_id, turn_status = _turn_marker(view)
    return json.dumps(
        {
            "version": 1,
            "backend": "vibe-unified",
            "location": str(view.current),
            "session_id": view.session_id,
            "generation": view.generation,
            "snapshot_sequence": view.snapshot_sequence,
            "sequence": view.sequence,
            "watermark": view.watermark,
            "usage": list(_usage(view)),
            "latest_turn_id": turn_id,
            "latest_turn_status": turn_status,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _cursor(payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(body).decode().rstrip("=")


def _decode_cursor(raw: str) -> dict | None:
    try:
        body = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        value = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict) or value.get("version") != 1:
        return None
    return value


class UnifiedVibeSource(Source):
    """A durable diff over Vibe's mutable public projection."""

    def __init__(
        self,
        observer: VibeObserver,
        *,
        cwd: str | None,
        session_id: str | None,
        after: float | None,
        session_provenance: str | TranscriptProvenance | None,
        known_location: str | None,
        source_checkpoint: str | None,
    ) -> None:
        self._observer = observer
        self._cwd = cwd
        self._session_id = session_id
        self._after = after
        self._session_provenance = normalize_provenance(session_provenance)
        self._known_location = Path(known_location) if known_location else None
        self._known_provenance = (
            self._session_provenance
            if self._known_location is not None
            else TranscriptProvenance.HEURISTIC
        )
        self._source_checkpoint = source_checkpoint
        self._view: UnifiedStoreView | None = None
        self._reader = UnifiedStoreReader()
        self._rows_view: UnifiedStoreView | None = None
        self._rows_cache: list[tuple[str, dict, int]] | None = None
        self._history_location: Path | None = None
        self._pending_view: UnifiedStoreView | None = None
        self._pending_attachment: UnifiedStoreView | None = None
        self._pending_checkpoint: str | None = None
        self._acknowledged_checkpoint = (
            source_checkpoint if _checkpoint(source_checkpoint) is not None else None
        )
        self._checkpoint_gap = False
        self.collision_domain = str(observer.root.resolve())

    @property
    def path(self) -> Path | None:
        if self._view is not None:
            return self._view.current
        return self._known_location

    def _inside_domain(self, path: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(self._observer.root.resolve())
        except ValueError:
            return False
        return True

    def _correlation(self, path: Path, session_id: str) -> str:
        if self._observer.isolated and self._inside_domain(path):
            return str(TranscriptProvenance.EXACT)
        if (
            self._known_location is not None
            and path == self._known_location
            and is_trusted_provenance(self._known_provenance)
        ):
            return str(self._known_provenance)
        if (
            self._session_provenance is TranscriptProvenance.EXACT
            and self._session_id == session_id
        ):
            return str(TranscriptProvenance.EXACT)
        return str(TranscriptProvenance.HEURISTIC)

    def correlation_for(self, path: Path, session_id: str | None) -> str:
        return self._correlation(path, session_id or self._session_id or "")

    async def _load(self, path: Path, **kwargs) -> UnifiedStoreView | None:
        """Load the store, polling through this source's incremental reader.

        The reader keeps one session's parsed generation documents plus a
        bounded chunk cache, so polling an unchanged store costs a CURRENT
        byte comparison and a journal lstat instead of a full reload.
        Historical loads and history reads bypass the reader state but still
        share its chunk cache.
        """
        if kwargs:
            return await self._load_fresh(path, **kwargs)
        return (await self._load_update(path)).view

    async def _load_update(self, path: Path) -> UnifiedStoreUpdate:
        import asyncio

        return await asyncio.to_thread(self._reader.load, path)

    async def _load_fresh(self, path: Path, **kwargs) -> UnifiedStoreView | None:
        import asyncio

        return await asyncio.to_thread(
            load_unified_store, path, chunk_cache=self._reader.chunk_cache, **kwargs
        )

    def _error_batch(self, exc: Exception, *, waiting: bool = False) -> Batch:
        code = getattr(exc, "code", None) or TRANSCRIPT_SOURCE_UNAVAILABLE_CODE
        return Batch(waiting=waiting, error_code=code, error=str(exc))

    def _attachment_path_error(self, path: Path) -> Batch | None:
        if not self._inside_domain(path):
            return Batch(
                waiting=True,
                error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
                error=f"Unified Vibe store {str(path)!r} is outside its transcript domain",
            )
        unavailable = trusted_location_unavailable_reason(
            location=str(path),
            provenance=str(self._known_provenance),
            domain=str(self._observer.root),
        )
        if unavailable is not None:
            return Batch(
                waiting=True,
                error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
                error=unavailable,
            )
        return None

    async def read(self) -> Batch:
        if self._pending_attachment is not None:
            raise RuntimeError("attachment must be committed or discarded before reading again")
        if self._pending_view is not None:
            raise RuntimeError("source checkpoint must be acknowledged before reading again")
        if self._checkpoint_gap:
            self._checkpoint_gap = False
            return Batch(
                error_code="vibe_unified_checkpoint_expired",
                error="Unified Vibe history advanced beyond its retained recovery journals",
            )
        if self._view is None:
            path = self._known_location
            if path is None:
                path = await self._observer.find_unified_transcript_async(
                    cwd=self._cwd, session_id=self._session_id, after=self._after
                )
            if path is None:
                return Batch(waiting=True)
            path_error = self._attachment_path_error(path)
            if path_error is not None:
                return path_error
            try:
                current = await self._load(path)
                if current is None:
                    return Batch(waiting=True)
                return await self._stage_attachment(current)
            except (OSError, UnifiedStoreError, ValueError) as exc:
                return self._error_batch(exc, waiting=True)
        try:
            update = await self._load_update(self._view.current)
        except (OSError, UnifiedStoreError, ValueError) as exc:
            return self._attachment_path_error(self._view.current) or self._error_batch(exc)
        current = update.view
        if current.sequence == self._view.sequence and current.watermark == self._view.watermark:
            return Batch()
        # The reader's change set is only meaningful relative to the exact
        # view this source last acknowledged; anything else — an attachment
        # restore, a discarded checkpoint, a rollback — must fall back to the
        # full fingerprint diff.
        changed = (
            update.changed_entry_ids
            if update.changed_entry_ids is not None and update.baseline is self._view
            else None
        )
        return self._diff(self._view, current, changed)

    async def _stage_attachment(self, current: UnifiedStoreView) -> Batch:
        baseline = current
        saved = _checkpoint(self._source_checkpoint)
        last_event: Event | None = None
        status: Status | None = None
        if saved is not None and (
            same_location(saved["location"], str(current.current))
            and saved["session_id"] == current.session_id
        ):
            saved_watermark = int(saved["watermark"])
            if saved_watermark > current.watermark:
                return Batch(
                    waiting=True,
                    error_code="vibe_unified_store_invalid",
                    error="Unified Vibe projection watermark moved backwards",
                )
            if saved_watermark < current.watermark:
                restored = await self._load(
                    current.current,
                    at_sequence=int(saved["sequence"]),
                    generation_hint=str(saved["generation"]),
                )
                if restored is not None and restored.watermark == saved_watermark:
                    baseline = restored
                else:
                    self._checkpoint_gap = True
                    self._pending_checkpoint = _encode_checkpoint(current)
                    self._pending_view = current
            elif (
                int(saved["sequence"]) != current.sequence
                or str(saved["generation"]) != current.generation
            ):
                # Non-projection journal records still advance the durable recovery point.
                self._pending_checkpoint = _encode_checkpoint(current)
                self._pending_view = current
            # An acknowledged checkpoint already applied attach-time semantics.
        else:
            last_event = self._turn_boundary(current, previous=None)
            status = _source_status(current)
            self._pending_checkpoint = _encode_checkpoint(current)
            self._pending_view = current
        self._pending_attachment = baseline
        return Batch(
            attached=Attachment(
                location=str(current.current),
                session_id=current.session_id,
                skipped=len(_entries(current)),
                last_event=last_event,
                status=status,
                point=StreamPoint(
                    stream_id=logical_stream_id(current.current, current.session_id),
                    position=current.watermark,
                ),
                correlation=self._correlation(current.current, current.session_id),
                collision_domain=self.collision_domain,
            ),
        )

    def _diff(
        self,
        previous: UnifiedStoreView,
        current: UnifiedStoreView,
        changed_entry_ids: frozenset[str] | None = None,
    ) -> Batch:
        """Project what moved between two views of the same session.

        ``changed_entry_ids`` is the reader's by-construction change set for
        ``current`` relative to ``previous``: entries whose ids it does not
        name share their entry object with the baseline, so their fingerprint
        comparison — the dominant cost of a long history — is skipped rather
        than re-run for every entry each tick. ``None`` means the change set
        is unknown (or ``previous`` may not be the reader's baseline) and
        only the full fingerprint diff is correct.
        """
        old_rows = self._rows(previous)
        new_rows = self._rows(current)
        old = {identity: entry for identity, entry, _index in old_rows}
        events: list[Event] = []
        facts: list[TrajectoryFact] = []
        baseline_events: list[Event] = []
        cwd = _runtime_metadata(current).get("cwd")
        cwd = cwd if isinstance(cwd, str) else self._cwd
        for identity, entry, index in new_rows:
            prior = old.get(identity)
            if prior is not None and not self._entry_changed(entry, prior, changed_entry_ids):
                continue
            parsed = project_unified_entry(
                entry,
                identity=identity,
                index=index,
                watermark=current.watermark,
                source_sequence=current.sequence,
                cwd=cwd,
                previous=prior,
            )
            events.extend(parsed.events)
            facts.extend(parsed.trajectory)
            baseline_events.extend(parsed.baseline_events)
        boundary = self._turn_boundary(current, previous=previous)
        if boundary is not None:
            matched = False
            if boundary.kind is EventKind.ASSISTANT:
                for index in range(len(events) - 1, -1, -1):
                    event = events[index]
                    if event.kind is EventKind.ASSISTANT and event.turn_id == boundary.turn_id:
                        events[index] = replace(event, turn_end=True)
                        matched = True
                        break
            if not matched:
                events.append(boundary)
        usage_event = self._usage_delta(previous, current)
        if usage_event is not None:
            events.append(usage_event)
        durable_usage = self._durable_usage_fact(current, previous=previous)
        if durable_usage is not None:
            facts.append(durable_usage)
        old_status = _source_status(previous)
        new_status = _source_status(current)
        status = new_status if new_status != old_status else None
        self._pending_view = current
        self._pending_checkpoint = _encode_checkpoint(current)
        return Batch(
            events=events,
            progressed=current.sequence != previous.sequence
            or current.watermark != previous.watermark,
            status=status,
            trajectory=facts,
            trajectory_events=baseline_events,
        )

    def _rows(self, view: UnifiedStoreView) -> list[tuple[str, dict, int]]:
        """Indexed rows for one view, computed once per view object.

        Consecutive views of an unchanged store share the view object, and
        the reader keeps untouched entry objects shared between consecutive
        views, so the cache is the load-bearing half of the fast path.
        """
        rows = self._rows_cache
        if self._rows_view is view and rows is not None:
            return rows
        rows = self._indexed_entries(view)
        self._rows_view = view
        self._rows_cache = rows
        return rows

    @staticmethod
    def _entry_changed(entry: dict, prior: dict, changed_entry_ids: frozenset[str] | None) -> bool:
        """Whether an entry with a known prior occurrence actually changed.

        Under the reader's change set, an id the applied journal records never
        touched is the same object as its prior occurrence by construction,
        so the fingerprint comparison is skipped; a touched id is compared as
        usual, which also leaves an appended duplicate of unchanged content
        unprojected.
        """
        if changed_entry_ids is not None and entry.get("id") not in changed_entry_ids:
            return False
        return entry_fingerprint(prior) != entry_fingerprint(entry)

    @staticmethod
    def _durable_usage_fact(
        view: UnifiedStoreView, *, previous: UnifiedStoreView | None
    ) -> TrajectoryFact | None:
        """The session's durable token totals as one stable trajectory record.

        The store persists session totals only, so there is no per-request
        attribution to invent: this is a single USAGE record under a stable
        native id, re-issued at the current watermark whenever the totals or
        the active model move. Merged trajectory caches keep the highest
        revision, so a cold history page and a live diff agree on one total
        instead of a warm-viewer delta double-counting a reloaded total.
        ``previous`` suppresses re-issue only on the live path, where the
        reader guarantees the two views are consecutive.
        """
        if not _has_durable_usage(view):
            return None
        if (
            previous is not None
            and _has_durable_usage(previous)
            and _usage(previous) == _usage(view)
            and _active_model(previous) == _active_model(view)
        ):
            return None
        prompt, completion, cached = _usage(view)
        return replace(
            session_usage_fact(
                input_tokens=max(0, prompt - cached),
                output_tokens=completion,
                cache_read_tokens=cached,
                model=_active_model(view),
                revision=view.watermark,
            ),
            source_offset=view.sequence,
        )

    @staticmethod
    def _indexed_entries(view: UnifiedStoreView) -> list[tuple[str, dict, int]]:
        occurrences: dict[str, int] = {}
        out: list[tuple[str, dict, int]] = []
        for index, entry in enumerate(_entries(view)):
            raw = entry.get("id")
            if not isinstance(raw, str) or not raw:
                continue
            occurrence = occurrences.get(raw, 0) + 1
            occurrences[raw] = occurrence
            identity = entry_identity(entry, occurrence)
            if identity is not None:
                out.append((identity, entry, index))
        return out

    @staticmethod
    def _turn_boundary(
        current: UnifiedStoreView, *, previous: UnifiedStoreView | None
    ) -> Event | None:
        turn_id, status = _turn_marker(current)
        if turn_id is None or status not in _TERMINAL_TURNS:
            return None
        if previous is not None and _turn_marker(previous) == (turn_id, status):
            return None
        text = ""
        for entry in reversed(_entries(current)):
            if (
                entry.get("type") == "message"
                and entry.get("role") == "assistant"
                and entry.get("turnId") == turn_id
            ):
                content = entry.get("content")
                if isinstance(content, list):
                    text = "\n\n".join(
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict)
                        and block.get("type") == "text"
                        and isinstance(block.get("text"), str)
                    )
                break
        turn = _latest_turn(current)
        if status == "failed":
            failure = turn.get("error")
            if isinstance(failure, dict) and isinstance(failure.get("message"), str):
                text = failure["message"]
        timestamp = _nonnegative_int(turn.get("completedAt"))
        ts = timestamp / 1000 if timestamp is not None else None
        return Event(
            kind=EventKind.ERROR if status == "failed" else EventKind.ASSISTANT,
            text=clip(text),
            raw_text=text,
            ts=ts,
            turn_end=True,
            turn_id=turn_id,
            raw_index=max(0, len(_entries(current)) - 1),
            source_offset=current.sequence,
        )

    @staticmethod
    def _usage_delta(previous: UnifiedStoreView, current: UnifiedStoreView) -> Event | None:
        old_prompt, old_completion, old_cached = _usage(previous)
        prompt, completion, cached = _usage(current)
        if prompt < old_prompt or completion < old_completion or cached < old_cached:
            return None
        d_prompt = prompt - old_prompt
        d_completion = completion - old_completion
        d_cached = cached - old_cached
        if d_prompt == 0 and d_completion == 0 and d_cached == 0:
            return None
        model = _runtime_metadata(current).get("active_model")
        model = model if isinstance(model, str) and model else None
        return Event(
            kind=EventKind.ASSISTANT,
            usage=TokenUsage(
                model=model,
                input_tokens=max(0, d_prompt - d_cached),
                output_tokens=d_completion,
                cache_read_input_tokens=d_cached,
                idempotency_key=(
                    f"vibe-unified:{old_prompt}:{old_completion}:{old_cached}"
                    f"->{prompt}:{completion}:{cached}"
                ),
            ),
            source_offset=current.sequence,
        )

    async def refresh(self) -> Batch:
        if self._view is None:
            return await self.read()
        candidate = await self._observer.find_unified_transcript_async(
            cwd=self._cwd, session_id=None, after=self._after
        )
        if candidate is None or candidate == self._view.current:
            return Batch()
        try:
            view = await self._load(candidate)
            return await self._stage_attachment(view) if view is not None else Batch()
        except (OSError, UnifiedStoreError, ValueError) as exc:
            return self._error_batch(exc)

    def commit_attachment(self) -> None:
        if self._pending_attachment is None:
            raise RuntimeError("no Unified Vibe attachment is pending")
        self._view = self._pending_attachment
        self._known_location = self._view.current
        self._session_id = self._view.session_id
        self._known_provenance = normalize_provenance(
            self._correlation(self._view.current, self._view.session_id)
        )
        self._pending_attachment = None

    def discard_attachment(self) -> None:
        if self._pending_attachment is None:
            raise RuntimeError("no Unified Vibe attachment is pending")
        self._pending_attachment = None
        self._pending_view = None
        self._pending_checkpoint = None
        self._checkpoint_gap = False

    def revoke_attachment(self) -> None:
        self._view = None
        self._pending_view = None
        self._pending_attachment = None
        self._known_location = None
        self._session_id = None
        self._history_location = None
        self._pending_checkpoint = None
        self._acknowledged_checkpoint = None
        self._checkpoint_gap = False
        self._reader.reset()
        self._rows_view = None
        self._rows_cache = None

    def admit_exact_location(self, *, location: str, session_id: str) -> ReceiptAdmission:
        path = Path(location)
        self._session_id = session_id
        self._session_provenance = TranscriptProvenance.EXACT
        self._known_location = path
        self._history_location = None
        self._known_provenance = TranscriptProvenance.EXACT
        if self._view is not None and self._view.current == path:
            return "accepted"
        self._view = None
        return "staged"

    def source_checkpoint(self) -> str | None:
        return self._acknowledged_checkpoint

    def pending_source_checkpoint(self) -> str | None:
        return self._pending_checkpoint

    def acknowledge_source_checkpoint(self) -> None:
        if self._pending_checkpoint is None:
            return
        self._acknowledged_checkpoint = self._pending_checkpoint
        self._source_checkpoint = self._pending_checkpoint
        self._pending_checkpoint = None
        if self._pending_view is not None:
            self._view = self._pending_view
            self._pending_view = None

    def rollback_source_checkpoint(self) -> None:
        self._pending_checkpoint = None
        self._pending_view = None

    async def _history_view(self) -> tuple[UnifiedStoreView | None, bool, str | None]:
        pinned = self._known_location is not None
        path = self.path or self._known_location or self._history_location
        if path is None:
            path = await self._observer.find_unified_transcript_async(
                cwd=self._cwd, session_id=self._session_id, after=self._after
            )
        if path is None:
            return None, pinned, None
        try:
            view = await self._load(path)
        except (OSError, UnifiedStoreError, ValueError) as exc:
            return None, pinned, str(exc)
        if view is not None:
            self._history_location = view.current
            self._session_id = view.session_id
        return view, pinned, None

    async def history(self, *, last_n: int) -> History:
        view, pinned, error = await self._history_view()
        if view is None:
            return History(
                error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE if error else None,
                error=error,
                pinned=pinned,
            )
        events, _facts = self._project_history(view, 0, len(_entries(view)), clip_text=False)
        return History(
            location=str(view.current),
            events=events[-last_n:] if last_n > 0 else events,
            correlation=self._correlation(view.current, view.session_id),
            collision_domain=self.collision_domain,
            pinned=pinned,
        )

    async def history_page(  # noqa: PLR0912
        self,
        *,
        before: str | None = None,
        snapshot: str | None = None,
        limit: int = TRAJECTORY_PAGE_RECORD_LIMIT,
        include_full_text: bool = False,
    ) -> HistoryPage:
        if type(limit) is not int or limit <= 0:
            return HistoryPage(
                error_code="invalid_limit", error="history page limit must be positive"
            )
        if before is not None and snapshot is not None:
            return HistoryPage(
                error_code="history_cursor_invalid",
                error="history page accepts either an older cursor or a snapshot cursor",
            )
        limit = min(limit, TRAJECTORY_PAGE_RECORD_LIMIT)
        raw_cursor = before if before is not None else snapshot
        payload = _decode_cursor(raw_cursor) if raw_cursor is not None else None
        if raw_cursor is not None and payload is None:
            return HistoryPage(error_code="history_cursor_invalid", error="invalid history cursor")
        view: UnifiedStoreView | None
        pinned = self._known_location is not None
        if payload is None:
            view, pinned, error = await self._history_view()
            if view is None:
                return HistoryPage(
                    error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE if error else None,
                    error=error,
                    pinned=pinned,
                )
            end = len(_entries(view))
        else:
            path = self.path or self._known_location or self._history_location
            if path is None or payload.get("session_id") != self._session_id:
                return HistoryPage(
                    error_code="history_cursor_invalid", error="history cursor session mismatch"
                )
            try:
                view = await self._load(
                    path,
                    at_sequence=_nonnegative_int(payload.get("sequence")),
                    generation_hint=payload.get("generation"),
                )
            except (OSError, UnifiedStoreError, ValueError) as exc:
                return HistoryPage(
                    error_code="history_cursor_invalid", error=str(exc), pinned=pinned
                )
            if view is None or view.watermark != payload.get("watermark"):
                return HistoryPage(
                    error_code="history_snapshot_expired",
                    error="Unified Vibe history snapshot is no longer retained",
                    pinned=pinned,
                )
            cursor_end = _nonnegative_int(payload.get("before"))
            if cursor_end is None or cursor_end > len(_entries(view)):
                return HistoryPage(
                    error_code="history_cursor_invalid", error="history cursor boundary is invalid"
                )
            end = cursor_end
        durable_usage = self._durable_usage_fact(view, previous=None)
        reserved_facts = 1 if durable_usage is not None else 0
        selected_start = end
        event_count = fact_count = 0
        for row_start in range(end - 1, -1, -1):
            row_events, row_facts = self._project_history(
                view, row_start, row_start + 1, clip_text=True
            )
            row_too_large = len(row_events) > limit or len(row_facts) > limit
            would_overflow = (
                event_count + len(row_events) > limit
                or fact_count + len(row_facts) + reserved_facts > limit
            )
            if row_too_large and not include_full_text:
                if selected_start == end:
                    return HistoryPage(
                        error_code="history_record_too_large",
                        error="one Unified Vibe entry exceeds the history page limit",
                        pinned=pinned,
                    )
                break
            if would_overflow and selected_start != end:
                break
            selected_start = row_start
            event_count += len(row_events)
            fact_count += len(row_facts)
            if would_overflow:
                break
        start = selected_start
        events, facts = self._project_history(view, start, end, clip_text=True)
        if durable_usage is not None:
            facts.append(durable_usage)
        full_events = None
        if include_full_text:
            full_events, _ = self._project_history(view, start, end, clip_text=False)
        base = {
            "version": 1,
            "session_id": view.session_id,
            "generation": view.generation,
            "sequence": view.sequence,
            "watermark": view.watermark,
        }
        snapshot_cursor = _cursor({**base, "before": end})
        older = _cursor({**base, "before": start}) if start > 0 else None
        return HistoryPage(
            location=str(view.current),
            events=events[-limit:],
            complete_events=full_events[-limit:] if full_events is not None else None,
            trajectory=facts[-limit:],
            trajectory_events=(),
            cursor=snapshot_cursor,
            snapshot_cursor=snapshot_cursor,
            older_cursor=older,
            has_older=start > 0,
            provenance=self._correlation(view.current, view.session_id),
            collision_domain=self.collision_domain,
            pinned=pinned,
        )

    def _project_history(
        self, view: UnifiedStoreView, start: int, end: int, *, clip_text: bool
    ) -> tuple[list[Event], list]:
        events: list[Event] = []
        facts: list[TrajectoryFact] = []
        occurrences: dict[str, int] = {}
        cwd = _runtime_metadata(view).get("cwd")
        cwd = cwd if isinstance(cwd, str) else self._cwd
        for index, entry in enumerate(_entries(view)[:end]):
            raw = entry.get("id")
            if not isinstance(raw, str) or not raw:
                continue
            occurrence = occurrences.get(raw, 0) + 1
            occurrences[raw] = occurrence
            if index < start:
                continue
            identity = entry_identity(entry, occurrence)
            if identity is None:
                continue
            parsed = project_unified_entry(
                entry,
                identity=identity,
                index=index,
                watermark=view.watermark,
                source_sequence=view.sequence,
                cwd=cwd,
                previous=None,
                clip_text=clip_text,
            )
            events.extend(parsed.events)
            facts.extend(parsed.trajectory)
        return events, facts

    async def aclose(self) -> None:
        return


__all__ = ["UnifiedVibeSource"]
