"""Mutable source for Vibe's experimental Unified Session Store."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from theater.constants.trajectory import TRAJECTORY_PAGE_RECORD_LIMIT
from theater.harness.contracts.events import Event, EventKind, TokenUsage, TurnTerminal, clip
from theater.harness.contracts.source import (
    NATIVE_TURN_ID_MAX_CHARS,
    NATIVE_TURN_IDS_MAX,
    Attachment,
    Batch,
    History,
    HistoryPage,
    NativeTurnState,
    ReceiptAdmission,
    Source,
    StreamPoint,
)
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.models import Status
from theater.provenance import TranscriptProvenance, is_trusted_provenance, normalize_provenance
from theater.trajectory.enums import CostProvenance
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
    same_location,
    trusted_location_unavailable_reason,
)

from .trajectory import session_usage_fact
from .unified_history import UnifiedHistoryReader, _entries, _nonnegative_int, _runtime_metadata
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
from .unified_store_turns import turn_changes_between
from .usage import _configured_model, _cost_from_prices

if TYPE_CHECKING:
    from .observer import VibeObserver

_CHECKPOINT_MAX_BYTES = 4096
_TERMINAL_TURNS = frozenset({"completed", "failed", "interrupted"})
_BUSY_SESSIONS = frozenset({"running", "blocked"})
_TERMINAL_OUTCOMES = {
    "completed": TurnTerminal.COMPLETED,
    "failed": TurnTerminal.FAILED,
    "interrupted": TurnTerminal.INTERRUPTED,
}


class _ReaderReplaced(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _DecodedCheckpoint:
    raw: str
    location: str
    session_id: str
    generation: str
    snapshot_sequence: int
    sequence: int
    watermark: int
    usage: tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class _StagedAdvancement:
    view: UnifiedStoreView
    checkpoint: str
    initial_usage: bool = False

    @classmethod
    def for_view(cls, view: UnifiedStoreView, *, initial_usage: bool = False) -> _StagedAdvancement:
        return cls(view, _encode_checkpoint(view), initial_usage)


@dataclass(frozen=True, slots=True)
class _StagedAttachment:
    baseline: UnifiedStoreView
    unproven: bool = False


def _checkpoint(raw: str | None) -> _DecodedCheckpoint | None:
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
    return _DecodedCheckpoint(
        raw=raw,
        location=value["location"],
        session_id=value["session_id"],
        generation=value["generation"],
        snapshot_sequence=value["snapshot_sequence"],
        sequence=value["sequence"],
        watermark=value["watermark"],
        usage=tuple(value["usage"]),
    )


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


def _pending_input_status(view: UnifiedStoreView) -> Status | None:
    """Keep native blocking authoritative between projection changes."""
    status = _source_status(view)
    return status if status is Status.AWAITING_INPUT else None


def _failure_text(failure: object) -> str:
    """Name a failed turn even when Vibe leaves ``message`` blank (e.g. a model ReadTimeout)."""
    if isinstance(failure, str):
        return failure.strip()
    if not isinstance(failure, dict):
        return ""
    message = failure.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    details = failure.get("details")
    details = details if isinstance(details, dict) else {}
    code, exception = failure.get("code"), details.get("exceptionType")
    route = "/".join(
        value for value in (details.get("provider"), details.get("model")) if isinstance(value, str)
    )
    text = ": ".join(value for value in (code, exception) if isinstance(value, str) and value)
    return f"{text} ({route})" if text and route else text


def _marker(turn: dict) -> tuple[str | None, str | None]:
    turn_id = turn.get("id")
    status = turn.get("status")
    return (
        turn_id if isinstance(turn_id, str) and turn_id else None,
        status if isinstance(status, str) and status else None,
    )


def _turn_marker(view: UnifiedStoreView) -> tuple[str | None, str | None]:
    return _marker(_latest_turn(view))


def _same_point(left: UnifiedStoreView | None, right: UnifiedStoreView | None) -> bool:
    return (
        left is not None
        and right is not None
        and (left.session_id, left.sequence, left.watermark)
        == (right.session_id, right.sequence, right.watermark)
    )


def _native_id(value: object) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= NATIVE_TURN_ID_MAX_CHARS else None


def _turn_state(
    view: UnifiedStoreView, turns: tuple[dict, ...] = (), *, complete: bool = True
) -> NativeTurnState:
    """Vibe's own turn fact: a turn in progress, queued input or a busy session is open work."""
    turn_id, status = _turn_marker(view)
    queue = view.snapshot.get("turnQueue")
    items = queue.get("items") if isinstance(queue, dict) else None
    items = items if isinstance(items, list) else []
    queued = [
        item_id
        for item in items
        if isinstance(item, dict) and (item_id := _native_id(item.get("id"))) is not None
    ]
    session_status = _session(view).get("status")
    session_type = session_status.get("type") if isinstance(session_status, dict) else None
    origins = {
        (origin_turn, item): None
        for turn in (*turns, _latest_turn(view))
        if (origin_turn := _native_id(turn.get("id"))) is not None
        and (item := _native_id(turn.get("queueItemId"))) is not None
    }
    return NativeTurnState(
        turn_id=_native_id(turn_id),
        open=status == "in_progress" or bool(items) or session_type in _BUSY_SESSIONS,
        queued=tuple(queued[:NATIVE_TURN_IDS_MAX]),
        origins=tuple(origins)[-NATIVE_TURN_IDS_MAX:],
        complete=complete,
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
        count_initial: bool = False,
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
        self._checkpoint = _checkpoint(source_checkpoint)
        self._count_initial = count_initial
        self._view: UnifiedStoreView | None = None
        self._reader = UnifiedStoreReader()
        self._rows_view: UnifiedStoreView | None = None
        self._rows_cache: list[tuple[str, dict, int]] | None = None
        self._pending_advancement: _StagedAdvancement | None = None
        self._pending_attachment: _StagedAttachment | None = None
        self._checkpoint_gap = False
        #: Turn ends since the acknowledged point could not be replayed; every batch says so,
        #: and stages a checkpoint, until the observer has applied one and acknowledged it.
        self._unproven = False
        self.collision_domain = str(observer.root.resolve())
        self._history = UnifiedHistoryReader(self)

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

    async def _load_current(self, path: Path) -> UnifiedStoreView | None:
        return (await self._load_update(path)).view

    async def _load_update(self, path: Path) -> UnifiedStoreUpdate:
        import asyncio

        reader = self._reader
        update = await asyncio.to_thread(reader.load, path)
        if reader is not self._reader:
            raise _ReaderReplaced
        return update

    async def _load_recovery(
        self, path: Path, *, at_sequence: int | None, generation_hint: str | None
    ) -> UnifiedStoreView | None:
        import asyncio

        reader = self._reader
        view = await asyncio.to_thread(
            load_unified_store,
            path,
            chunk_cache=reader.chunk_cache,
            at_sequence=at_sequence,
            generation_hint=generation_hint,
        )
        if reader is not self._reader:
            raise _ReaderReplaced
        return view

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
        pending = self._before_read()
        if pending is not None:
            return pending
        if self._view is None:
            return await self._read_attachment()
        return await self._read_progress()

    def _before_read(self) -> Batch | None:
        if self._pending_attachment is not None:
            raise RuntimeError("attachment must be committed or discarded before reading again")
        if self._pending_advancement is not None:
            raise RuntimeError("source checkpoint must be acknowledged before reading again")
        if self._checkpoint_gap:
            self._checkpoint_gap = False
            return Batch(
                error_code="vibe_unified_checkpoint_expired",
                error="Unified Vibe history advanced beyond its retained recovery journals",
            )
        if self._view is None or not self._count_initial:
            return None
        usage_event = self._usage_since((0, 0, 0), self._view)
        if usage_event is None:
            self._count_initial = False
            return None
        usage_fact = self._durable_usage_fact(self._view, previous=None)
        self._pending_advancement = _StagedAdvancement.for_view(self._view, initial_usage=True)
        return Batch(
            events=[usage_event],
            progressed=True,
            status=_pending_input_status(self._view),
            trajectory=(usage_fact,) if usage_fact is not None else (),
            turn_state=_turn_state(self._view, complete=not self._unproven),
        )

    async def _read_attachment(self) -> Batch:
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
            current = await self._load_current(path)
            if current is None:
                return Batch(waiting=True)
            return await self._stage_attachment(current)
        except _ReaderReplaced:
            return Batch(waiting=True)
        except (OSError, UnifiedStoreError, ValueError) as exc:
            return self._error_batch(exc, waiting=True)

    async def _read_progress(self) -> Batch:
        assert self._view is not None
        try:
            update = await self._load_update(self._view.current)
        except _ReaderReplaced:
            return Batch()
        except (OSError, UnifiedStoreError, ValueError) as exc:
            return self._attachment_path_error(self._view.current) or self._error_batch(exc)
        current = update.view
        if current.sequence < self._view.sequence or current.watermark < self._view.watermark:
            # An append-only journal went backwards: keep the acknowledged point and its open
            # turn, and report the turn ends in between as unprovable.
            self._unproven = True
            return Batch(
                error_code="vibe_unified_store_invalid",
                error=(
                    f"Unified Vibe history moved backwards from sequence {self._view.sequence} "
                    f"to {current.sequence}; keeping the acknowledged point"
                ),
                turn_state=_turn_state(self._view, complete=False),
            )
        if current.sequence == self._view.sequence and current.watermark == self._view.watermark:
            if self._unproven:
                self._pending_advancement = _StagedAdvancement.for_view(current)
            return Batch(
                status=_pending_input_status(current),
                turn_state=_turn_state(current, complete=not self._unproven),
            )
        # The reader's change set is only meaningful relative to the exact
        # view this source last acknowledged; anything else — an attachment
        # restore, a discarded checkpoint, a rollback — must fall back to the
        # full fingerprint diff.
        changed = (
            update.changed_entry_ids
            if update.changed_entry_ids is not None and update.baseline is self._view
            else None
        )
        turns = update.turns if _same_point(update.baseline, self._view) else None
        if turns is None:
            import asyncio

            # A restored checkpoint, a rolled-back batch or a reload: replay the retained
            # journals from the acknowledged view instead of trusting the reader's own baseline.
            acknowledged = self._view
            turns = await asyncio.to_thread(turn_changes_between, acknowledged, current)
            if self._view is not acknowledged:
                return Batch()
        return self._diff(self._view, current, changed, turns=turns)

    async def _stage_attachment(self, current: UnifiedStoreView) -> Batch:
        baseline = current
        saved = self._checkpoint
        last_event: Event | None = None
        status: Status | None = None
        attach_unproven = False
        if saved is not None and (
            same_location(saved.location, str(current.current))
            and saved.session_id == current.session_id
        ):
            saved_watermark = saved.watermark
            if saved_watermark > current.watermark or saved.sequence > current.sequence:
                return Batch(
                    waiting=True,
                    error_code="vibe_unified_store_invalid",
                    error="Unified Vibe projection watermark moved backwards",
                )
            if saved_watermark < current.watermark:
                restored = await self._load_recovery(
                    current.current,
                    at_sequence=saved.sequence,
                    generation_hint=saved.generation,
                )
                if restored is not None and restored.watermark == saved_watermark:
                    baseline = restored
                else:
                    # Reported on the attachment itself, before any checkpoint can advance.
                    attach_unproven = True
                    self._pending_advancement = _StagedAdvancement.for_view(current)
            elif saved.sequence != current.sequence or saved.generation != current.generation:
                # Non-projection journal records still advance the durable recovery point.
                self._pending_advancement = _StagedAdvancement.for_view(current)
            # Do not replay attach-time events; pending input is still a current state.
            status = _pending_input_status(baseline)
        else:
            last_event = self._turn_boundary(current, previous=None)
            status = _source_status(current)
            self._pending_advancement = _StagedAdvancement.for_view(current)
        self._pending_attachment = _StagedAttachment(baseline, unproven=attach_unproven)
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
            turn_state=_turn_state(
                current, complete=not (self._unproven or self._pending_attachment.unproven)
            ),
        )

    def _diff(
        self,
        previous: UnifiedStoreView,
        current: UnifiedStoreView,
        changed_entry_ids: frozenset[str] | None = None,
        *,
        turns: tuple[dict, ...] | None = (),
    ) -> Batch:
        """Project changed entries; ``None`` means the change set is unknown.

        ``turns`` are the folded ``latestTurn`` changes since ``previous``, each terminal one
        ending its own turn in order; None (unprovable) is reported, never taken as none.
        """
        old_rows = self._rows(previous)
        new_rows = self._rows(current)
        old = {identity: (entry, index) for identity, entry, index in old_rows}
        events: list[Event] = []
        facts: list[TrajectoryFact] = []
        baseline_events: list[Event] = []
        cwd = _runtime_metadata(current).get("cwd")
        cwd = cwd if isinstance(cwd, str) else self._cwd
        for identity, entry, index in new_rows:
            prior, prior_index = old.get(identity, (None, None))
            if (
                prior is not None
                and prior_index == index
                and not self._entry_changed(entry, prior, changed_entry_ids)
            ):
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
        known = () if turns is None else turns
        self._unproven = self._unproven or turns is None
        events = self._place_boundaries(
            events,
            self._turn_boundaries(current, previous=previous, turns=known),
            latest_turn_id=_turn_marker(current)[0],
        )
        usage_event = self._usage_delta(previous, current)
        if usage_event is not None:
            events.append(usage_event)
        durable_usage = self._durable_usage_fact(current, previous=previous)
        if durable_usage is not None:
            facts.append(durable_usage)
        old_status = _source_status(previous)
        new_status = _source_status(current)
        status = new_status if new_status != old_status else _pending_input_status(current)
        self._pending_advancement = _StagedAdvancement.for_view(current)
        return Batch(
            events=events,
            progressed=current.sequence != previous.sequence
            or current.watermark != previous.watermark,
            status=status,
            trajectory=facts,
            trajectory_events=baseline_events,
            turn_state=_turn_state(current, known, complete=not self._unproven),
        )

    def _rows(self, view: UnifiedStoreView) -> list[tuple[str, dict, int]]:
        """Index each view object once."""
        rows = self._rows_cache
        if self._rows_view is view and rows is not None:
            return rows
        rows = self._indexed_entries(view)
        self._rows_view = view
        self._rows_cache = rows
        return rows

    @staticmethod
    def _entry_changed(entry: dict, prior: dict, changed_entry_ids: frozenset[str] | None) -> bool:
        """Check one entry, skipping IDs the reader proved untouched."""
        if changed_entry_ids is not None and entry.get("id") not in changed_entry_ids:
            return False
        return entry_fingerprint(prior) != entry_fingerprint(entry)

    @staticmethod
    def _durable_usage_fact(
        view: UnifiedStoreView, *, previous: UnifiedStoreView | None
    ) -> TrajectoryFact | None:
        """Return one unattributed session-total usage record."""
        if not _has_durable_usage(view):
            return None
        if (
            previous is not None
            and _has_durable_usage(previous)
            and _usage(previous) == _usage(view)
        ):
            return None
        prompt, completion, cached = _usage(view)
        return replace(
            session_usage_fact(
                input_tokens=max(0, prompt - cached),
                output_tokens=completion,
                cache_read_tokens=cached,
                model=None,
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
        boundaries = UnifiedVibeSource._turn_boundaries(current, previous=previous)
        return boundaries[-1] if boundaries else None

    @staticmethod
    def _turn_boundaries(
        current: UnifiedStoreView,
        *,
        previous: UnifiedStoreView | None,
        turns: tuple[dict, ...] = (),
    ) -> list[Event]:
        """One end per turn that reached a terminal status since ``previous``, oldest first."""
        seen = {_turn_marker(previous)} if previous is not None else set()
        boundaries: list[Event] = []
        for turn in (*turns, _latest_turn(current)):
            marker = _marker(turn)
            if marker[0] is None or marker[1] not in _TERMINAL_TURNS or marker in seen:
                continue
            seen.add(marker)
            boundaries.append(UnifiedVibeSource._boundary_event(current, turn))
        return boundaries

    @staticmethod
    def _place_boundaries(
        events: list[Event], boundaries: list[Event], *, latest_turn_id: str | None
    ) -> list[Event]:
        """Mark or insert each end after its own turn's events, before any later turn's."""
        floor = 0
        for position, boundary in enumerate(boundaries):
            own = [i for i in range(floor, len(events)) if events[i].turn_id == boundary.turn_id]
            replies = [i for i in own if events[i].kind is EventKind.ASSISTANT]
            if boundary.kind is EventKind.ASSISTANT and replies:
                events[replies[-1]] = replace(
                    events[replies[-1]], turn_end=True, turn_terminal=boundary.turn_terminal
                )
                floor = own[-1] + 1
                continue
            later = {b.turn_id for b in boundaries[position + 1 :]} | {latest_turn_id}
            later.discard(boundary.turn_id)
            starts = (i for i in range(floor, len(events)) if events[i].turn_id in later)
            at = own[-1] + 1 if own else next(starts, len(events))
            events.insert(at, boundary)
            floor = at + 1
        return events

    @staticmethod
    def _boundary_event(current: UnifiedStoreView, turn: dict) -> Event:
        turn_id, status = _marker(turn)
        assert turn_id is not None and status in _TERMINAL_TURNS
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
        if status == "failed":
            text = _failure_text(turn.get("error")) or text
        timestamp = _nonnegative_int(turn.get("completedAt"))
        ts = timestamp / 1000 if timestamp is not None else None
        return Event(
            kind=EventKind.ERROR if status == "failed" else EventKind.ASSISTANT,
            text=clip(text),
            raw_text=text,
            ts=ts,
            turn_end=True,
            turn_terminal=_TERMINAL_OUTCOMES[status],
            turn_id=turn_id,
            raw_index=max(0, len(_entries(current)) - 1),
            source_offset=current.sequence,
        )

    @staticmethod
    def _usage_delta(previous: UnifiedStoreView, current: UnifiedStoreView) -> Event | None:
        return UnifiedVibeSource._usage_since(_usage(previous), current)

    @staticmethod
    def _usage_since(previous: tuple[int, int, int], current: UnifiedStoreView) -> Event | None:
        old_prompt, old_completion, old_cached = previous
        prompt, completion, cached = _usage(current)
        if prompt < old_prompt or completion < old_completion or cached < old_cached:
            return None
        d_prompt = prompt - old_prompt
        d_completion = completion - old_completion
        d_cached = cached - old_cached
        if d_prompt == 0 and d_completion == 0 and d_cached == 0:
            return None
        active_model = _runtime_metadata(current).get("active_model")
        model, provider, model_config = _configured_model(active_model)
        cost_usd = None
        if model_config is not None:
            cost_usd = _cost_from_prices(
                model_config.get("input_price"),
                model_config.get("output_price"),
                model_config.get("cached_price"),
                max(0, d_prompt - d_cached),
                d_completion,
                d_cached,
            )
        return Event(
            kind=EventKind.ASSISTANT,
            usage=TokenUsage(
                model=model,
                provider=provider,
                input_tokens=max(0, d_prompt - d_cached),
                output_tokens=d_completion,
                cache_read_input_tokens=d_cached,
                cost_usd=cost_usd,
                cost_provenance=(
                    CostProvenance.ESTIMATED if cost_usd is not None else CostProvenance.UNKNOWN
                ),
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
            view = await self._load_current(candidate)
            return await self._stage_attachment(view) if view is not None else Batch()
        except _ReaderReplaced:
            return Batch()
        except (OSError, UnifiedStoreError, ValueError) as exc:
            return self._error_batch(exc)

    def commit_attachment(self) -> None:
        if self._pending_attachment is None:
            raise RuntimeError("no Unified Vibe attachment is pending")
        self._view = self._pending_attachment.baseline
        self._known_location = self._view.current
        self._session_id = self._view.session_id
        self._known_provenance = normalize_provenance(
            self._correlation(self._view.current, self._view.session_id)
        )
        self._unproven = self._unproven or self._pending_attachment.unproven
        self._checkpoint_gap = self._checkpoint_gap or self._pending_attachment.unproven
        self._pending_attachment = None

    def discard_attachment(self) -> None:
        if self._pending_attachment is None:
            raise RuntimeError("no Unified Vibe attachment is pending")
        self._pending_attachment = None
        self._pending_advancement = None
        self._checkpoint_gap = False

    def revoke_attachment(self) -> None:
        self._view = None
        self._pending_advancement = None
        self._pending_attachment = None
        self._known_location = None
        self._session_id = None
        self._history.reset()
        self._checkpoint = None
        self._checkpoint_gap = False
        self._unproven = False
        self._reader = UnifiedStoreReader()
        self._rows_view = None
        self._rows_cache = None

    def admit_exact_location(self, *, location: str, session_id: str) -> ReceiptAdmission:
        path = Path(location)
        self._session_id = session_id
        self._session_provenance = TranscriptProvenance.EXACT
        self._known_location = path
        self._history.reset()
        self._known_provenance = TranscriptProvenance.EXACT
        if self._view is not None and self._view.current == path:
            return "accepted"
        self._view = None
        self._reader = UnifiedStoreReader()
        self._rows_view = None
        self._rows_cache = None
        return "staged"

    def source_checkpoint(self) -> str | None:
        return self._checkpoint.raw if self._checkpoint is not None else None

    def pending_source_checkpoint(self) -> str | None:
        pending = self._pending_advancement
        return pending.checkpoint if pending is not None else None

    def acknowledge_source_checkpoint(self) -> None:
        pending = self._pending_advancement
        if pending is None:
            return
        self._checkpoint = _checkpoint(pending.checkpoint)
        self._pending_advancement = None
        # Only an applied batch is acknowledged, and one carrying the unproven fact has had its
        # delivered jobs ended unverified first.
        self._unproven = False
        self._view = pending.view
        if pending.initial_usage:
            self._count_initial = False

    def rollback_source_checkpoint(self) -> None:
        self._pending_advancement = None

    async def history(self, *, last_n: int) -> History:
        pinned = self._known_location is not None
        try:
            return await self._history.history(last_n=last_n)
        except _ReaderReplaced:
            return History(pinned=pinned)

    async def history_page(
        self,
        *,
        before: str | None = None,
        snapshot: str | None = None,
        limit: int = TRAJECTORY_PAGE_RECORD_LIMIT,
        include_full_text: bool = False,
    ) -> HistoryPage:
        pinned = self._known_location is not None
        try:
            return await self._history.history_page(
                before=before,
                snapshot=snapshot,
                limit=limit,
                include_full_text=include_full_text,
            )
        except _ReaderReplaced:
            return HistoryPage(pinned=pinned)

    async def aclose(self) -> None:
        return


__all__ = ["UnifiedVibeSource"]
