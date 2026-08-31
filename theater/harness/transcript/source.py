"""TranscriptSource: tail an append-only transcript file.

The implementation of the file-backed ``Source``. Holds the byte offset, record
index and mtime that used to live on the observer's cursor. Nothing above it
knows the input is a file.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from theater.constants.trajectory import TRAJECTORY_PAGE_RECORD_LIMIT
from theater.harness.contracts.events import Event
from theater.harness.contracts.source import (
    Attachment,
    Batch,
    History,
    HistoryPage,
    IdentityLossEvidence,
    ReceiptAdmission,
    Source,
    SourceContractError,
    StreamPoint,
)
from theater.harness.contracts.trajectory import ParsedRecord, TrajectoryFact
from theater.harness.transcript.attachment import attach_point
from theater.harness.transcript.history import HistoryPageError, HistoryReader
from theater.provenance import (
    TranscriptProvenance,
    is_trusted_provenance,
    normalize_provenance,
)
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
    trusted_location_unavailable_reason,
)

if TYPE_CHECKING:
    from theater.harness.transcript.observer import TranscriptObserver

logger = logging.getLogger("theater.harness.source")


class TranscriptSource(Source):
    """Tail an append-only transcript file. What ``TranscriptObserver`` returns.

    Holds the byte offset, record index and mtime that used to live on the
    observer's cursor. Nothing above it knows the input is a file.
    """

    def __init__(
        self,
        observer: TranscriptObserver,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
        allow_refresh: bool = False,
        exact_attachments: bool = False,
        session_provenance: str | TranscriptProvenance | None = None,
        collision_domain: str | None = None,
        known_location: str | None = None,
    ) -> None:
        self._observer = observer
        self._cwd = cwd
        #: Updated when attach reveals the harness's own session id for re-attach.
        self._session_id = session_id
        self._after = after
        self._allow_refresh = allow_refresh
        self._exact_attachments = exact_attachments
        self._session_provenance = normalize_provenance(session_provenance)
        self.collision_domain = collision_domain
        self._domain_root = Path(collision_domain).resolve() if collision_domain else None
        self._known_location = Path(known_location) if known_location else None
        self._known_location_provenance = (
            self._session_provenance
            if self._known_location is not None
            else TranscriptProvenance.HEURISTIC
        )
        #: Proven locations with strength; held here so proof and trust agree.
        self._proven: dict[Path, TranscriptProvenance] = {}
        self.path: Path | None = None
        self.offset = 0
        self.index = 0
        self.mtime = 0
        self._pending: tuple[Path, int, int, int, str | None] | None = None
        #: Trusted pin must be absent twice before ENOENT becomes identity loss.
        self._missing_trusted_pin_once: Path | None = None
        #: One same-exact-session relocation lookup per missing-pin episode.
        self._relocation_attempted = False

    async def read(self) -> Batch:
        self._require_decision()
        if self.path is None:
            if reason := self._trusted_known_location_unavailable_reason():
                assert self._known_location is not None
                return await self._confirmed_missing_pin_batch(self._known_location, reason)
            try:
                attached = await self._attach()
            except OSError as exc:
                if self._known_location_is_trusted() and exc.errno == errno.ENOENT:
                    assert self._known_location is not None
                    return await self._confirmed_missing_pin_batch(
                        self._known_location,
                        f"trusted transcript pin {str(self._known_location)!r} "
                        "no longer exists on disk",
                    )
                self._missing_trusted_pin_once = None
                self._relocation_attempted = False
                return self._source_unavailable_batch(exc)
            self._missing_trusted_pin_once = None
            self._relocation_attempted = False
            return Batch(attached=attached) if attached else Batch(waiting=True)
        try:
            batch = self._drain()
        except OSError as exc:
            if self._path_is_trusted_pin(self.path) and exc.errno == errno.ENOENT:
                return await self._confirmed_missing_pin_batch(
                    self.path,
                    f"trusted transcript pin {str(self.path)!r} no longer exists on disk",
                )
            if exc.errno == errno.ENOENT:
                # Heuristic transcript deleted or rotated; drop back to searching.
                self._missing_trusted_pin_once = None
                self._relocation_attempted = False
                self._known_location = None
                self._detach()
                return Batch(waiting=True)
            self._missing_trusted_pin_once = None
            self._relocation_attempted = False
            return self._source_unavailable_batch(exc)
        else:
            self._missing_trusted_pin_once = None
            self._relocation_attempted = False
            return batch

    async def refresh(self) -> Batch:
        """Propose the newest transcript if the harness started a new one.

        Located by cwd alone, ignoring the session id: a harness may rotate to
        a new stream while its initially discovered stream remains unchanged.

        The same path back means the agent is idle rather than rotated, and
        returns an empty batch so the screen and rescue timers keep counting.
        The relocate arm may throttle its own clock, but must not reset either
        of the other clocks.
        """
        self._require_decision()
        path = await self._proven_rotation()
        if path is None and self._allow_refresh:
            path = await self._locate(session_id=None)
        if path is None or path == self.path:
            return Batch()
        session_id = self._observer.session_id(path)
        if self._trusted_pin_is_being_replaced_by_guess(path, session_id):
            # Heuristic movement is never an attachment; probe reports it only.
            return Batch()
        logger.info("transcript rotated: %s -> %s", self.path, path)
        attached = await self._attach(path)
        return Batch(attached=attached) if attached else Batch()

    async def probe_identity_loss(self) -> IdentityLossEvidence | None:
        """Look for a newer heuristic candidate without staging it.

        The adapter owns the bounded search. This source supplies the accepted
        path and cursor mtime, then rejects trusted results: exact/proven
        rotations belong to :meth:`refresh`, while this channel exists only to
        support quarantine evidence.
        """
        if self.path is None or not self._path_is_trusted_pin(self.path):
            return None
        candidate = await asyncio.to_thread(
            self._observer.identity_loss_candidate,
            cwd=self._cwd,
            current=self.path,
            current_mtime_ns=self.mtime,
            after=self._after,
        )
        if candidate is None or candidate == self.path or not self._inside_domain(candidate):
            return None
        session_id = self._observer.session_id(candidate)
        if is_trusted_provenance(self.correlation_for(candidate, session_id)):
            return None
        return IdentityLossEvidence(location=str(candidate), session_id=session_id)

    def commit_attachment(self) -> None:
        """Make the observer-accepted candidate the live transcript."""
        if self._pending is None:
            raise RuntimeError("no transcript attachment is pending")
        path, offset, index, mtime, session_id = self._pending
        provenance = normalize_provenance(self.correlation_for(path, session_id))
        self.path, self.offset, self.index, self.mtime = path, offset, index, mtime
        if session_id:
            if provenance is not TranscriptProvenance.EXACT:
                # The id was read off a guessed file; an exact claim would launder it.
                self._session_provenance = TranscriptProvenance.HEURISTIC
            self._session_id = session_id
        self._known_location = path
        self._known_location_provenance = provenance
        self._pending = None

    def discard_attachment(self) -> None:
        """Reject a candidate while continuing to watch the accepted file."""
        if self._pending is None:
            raise RuntimeError("no transcript attachment is pending")
        self._pending = None

    def revoke_attachment(self) -> None:
        """Return to discovery after an exact claimant proves this was wrong."""
        self._pending = None
        self._detach()
        # The id came from the revoked file; retaining it would re-select it.
        self._session_id = None
        self._known_location = None
        self._known_location_provenance = TranscriptProvenance.HEURISTIC

    def admit_exact_location(self, *, location: str, session_id: str) -> ReceiptAdmission:
        """Use a receipt-proven path on the next read."""
        path = Path(location)
        self._pending = None
        self._session_id = session_id
        self._session_provenance = TranscriptProvenance.EXACT
        self._known_location = path
        self._known_location_provenance = TranscriptProvenance.EXACT
        self._proven[path] = TranscriptProvenance.EXACT
        if self.path == path:
            return "accepted"
        self._detach()
        return "staged"

    async def history(self, *, last_n: int) -> History:
        """Re-read the whole transcript with the text left unclipped.

        Located from scratch rather than reusing `self.path`, so this works on
        a source that has never polled — which is the normal case, since the
        caller opens one just for this.
        """
        pinned = self._known_location is not None
        path = self.path
        if path is None and self._known_location is not None:
            if reason := self._trusted_known_location_unavailable_reason():
                return History(
                    error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
                    error=reason,
                    pinned=True,
                )
            path = await self._upgraded(self._known_location)
        if path is None:
            path = await self._locate(session_id=self._session_id)
        if path is None:
            return History(pinned=pinned)
        if path_error := self._history_path_error(path, pinned=pinned):
            return path_error
        try:
            events = await asyncio.to_thread(
                self._read_all,
                path,
                strict=self._path_is_trusted_pin(path),
            )
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                return History(
                    error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
                    error=f"transcript source {str(path)!r} is unavailable: {exc}",
                    pinned=True,
                )
            return History(
                error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
                error=f"trusted transcript pin {str(path)!r} no longer exists on disk",
                pinned=True,
            )
        events = [event for event in events if not event.usage_only]
        return History(
            location=str(path),
            events=events[-last_n:] if last_n > 0 else events,
            correlation=self.correlation_for(path, self._observer.session_id(path)),
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
        """Read a bounded JSONL window without touching the live tail cursor."""
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
        page_cursor = before if before is not None else snapshot
        pinned = self._known_location is not None
        path = self.path
        if path is None and self._known_location is not None:
            if reason := self._trusted_known_location_unavailable_reason():
                return HistoryPage(
                    error_code=TRANSCRIPT_IDENTITY_LOST_CODE, error=reason, pinned=True
                )
            path = await self._upgraded(self._known_location)
        if path is None:
            path = await self._locate(session_id=self._session_id)
        if path is None:
            if page_cursor is not None:
                return HistoryPage(
                    error_code="history_cursor_invalid",
                    error="history cursor cannot be used because the transcript is unavailable",
                    pinned=pinned,
                )
            return HistoryPage(pinned=pinned)
        if path_error := self._history_path_error(path, pinned=pinned):
            if page_cursor is not None and path_error.error_code is None:
                return HistoryPage(
                    location=path_error.location,
                    error_code="history_cursor_invalid",
                    error="history cursor cannot be used because the transcript is unavailable",
                    provenance=path_error.correlation,
                    pinned=path_error.pinned,
                )
            return HistoryPage(
                location=path_error.location,
                error_code=path_error.error_code,
                error=path_error.error,
                provenance=path_error.correlation,
                pinned=path_error.pinned,
            )
        try:
            reader = self._history_reader()
            end, end_index, cursor_identity = (
                reader.decode_page_cursor(page_cursor, path)
                if page_cursor is not None
                else (None, None, None)
            )
            live_offset = self.offset if page_cursor is None and self.path == path else None
            live_index = self.index if page_cursor is None and self.path == path else None
            result = await asyncio.to_thread(
                reader.read_page,
                path,
                end=end,
                end_index=end_index if page_cursor is not None else live_index,
                live_offset=live_offset,
                limit=limit,
                expected_identity=cursor_identity,
                include_full_text=include_full_text,
            )
        except HistoryPageError as exc:
            return HistoryPage(error_code=exc.code, error=str(exc), pinned=pinned)
        except ValueError as exc:
            return HistoryPage(error_code="history_cursor_invalid", error=str(exc), pinned=pinned)
        except OSError as exc:
            if exc.errno == errno.ENOENT and self._path_is_trusted_pin(path):
                return HistoryPage(
                    error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
                    error=f"trusted transcript pin {str(path)!r} no longer exists on disk",
                    pinned=True,
                )
            return HistoryPage(
                error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
                error=f"transcript source {str(path)!r} is unavailable: {exc}",
                pinned=pinned,
            )
        session_id = self._observer.session_id(path)
        snapshot_cursor = reader.encode_page_cursor(
            path, result.page_end, result.page_end_index, result.identity
        )
        return HistoryPage(
            location=str(path),
            events=result.events,
            complete_events=result.complete_events if include_full_text else None,
            trajectory=result.facts,
            trajectory_events=result.trajectory_events,
            cursor=snapshot_cursor,
            snapshot_cursor=snapshot_cursor,
            older_cursor=(
                reader.encode_page_cursor(
                    path, result.start, result.start_index, result.older_identity
                )
                if 0 < result.start < result.page_end
                else None
            ),
            has_older=0 < result.start < result.page_end,
            provenance=self.correlation_for(path, session_id),
            collision_domain=self.collision_domain,
            pinned=pinned,
        )

    def _history_path_error(self, path: Path, *, pinned: bool) -> History | None:
        """Validate a history location without turning generic I/O into loss."""
        try:
            inside_domain = self._inside_domain(path)
        except OSError as exc:
            return History(
                error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
                error=f"transcript source {str(path)!r} is unavailable: {exc}",
                pinned=pinned,
            )
        if not inside_domain:
            if self._path_is_trusted_pin(path):
                return History(
                    error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
                    error=(
                        f"trusted transcript pin {str(path)!r} no longer exists inside its "
                        "trusted transcript domain"
                    ),
                    pinned=True,
                )
            return History(pinned=pinned)
        if pinned:
            try:
                path.stat()
            except OSError as exc:
                # Never replace an admitted historical location with a cwd guess.
                if self._path_is_trusted_pin(path) and exc.errno == errno.ENOENT:
                    return History(
                        error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
                        error=f"trusted transcript pin {str(path)!r} no longer exists on disk",
                        pinned=True,
                    )
                if exc.errno != errno.ENOENT:
                    return History(
                        error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
                        error=f"transcript source {str(path)!r} is unavailable: {exc}",
                        pinned=True,
                    )
                return History(pinned=True)
        return None

    def correlation_for(self, path: Path, session_id: str | None) -> str:
        """How well *path* is known to belong to this participant.

        A method, and the one place both `read` and `history` ask the
        question, because exactness is a property of the **location** rather
        than of the source. A subclass whose discovery sometimes proves
        ownership — and sometimes falls back to the same cwd scan as everyone
        else — cannot answer with a flag fixed at construction without
        claiming proof for the fallback.

        Three ways to be trusted are known here. ``exact_attachments`` says
        every candidate under this source's root has one possible owner by
        construction (a participant-isolated save directory). A location the
        observer's proof channel answered with is proven by definition, and is
        recorded here rather than trusted to the adapter. Exact session
        provenance says the id we were given was itself exact — a launch
        receipt, or an earlier exact proof already persisted — so a file
        carrying that id is the right one. Persisted proven/operator
        provenance is narrower: it trusts only the persisted known location.
        """
        if self._exact_attachments and self._inside_domain(path):
            return str(TranscriptProvenance.EXACT)
        if path in self._proven:
            return str(self._proven[path])
        if (
            self._known_location is not None
            and path == self._known_location
            and is_trusted_provenance(self._known_location_provenance)
        ):
            return str(self._known_location_provenance)
        if (
            self._session_provenance is TranscriptProvenance.EXACT
            and self._session_id is not None
            and session_id is not None
            and session_id == self._session_id
        ):
            return str(TranscriptProvenance.EXACT)
        return str(TranscriptProvenance.HEURISTIC)

    def _read_all(self, path: Path, *, strict: bool = False) -> list[Event]:
        events: list[Event] = []
        try:
            with path.open("rb") as fh:
                offset = 0
                for index, raw in enumerate(fh):
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
                        parsed = self._parse_record(line, index, clip_text=False)
                        decorated = self._decorate_parsed(parsed, offset)
                        events.extend(decorated.events)
                    offset += len(raw)
        except OSError:
            if strict:
                raise
            # A transcript that vanished mid-read is the same non-event here.
            return []
        return events

    def _history_reader(self) -> HistoryReader:
        return HistoryReader(
            parse_record=lambda line, index: self._parse_record(line, index, clip_text=False),
            decorate_parsed=self._decorate_parsed,
            prepare_history_parse=self._prepare_history_parse,
        )

    def _prepare_history_parse(self, fh: BinaryIO, start: int) -> None:
        """Let a stateful adapter seed bounded context before forward parsing."""

    def _parse_record(self, line: str, index: int, *, clip_text: bool) -> ParsedRecord:
        missing = object()
        parser = getattr(self._observer, "parse_record", missing)
        if parser is missing:
            return ParsedRecord(
                events=tuple(self._observer.parse(line, index, clip_text=clip_text))
            )
        if not callable(parser):
            raise SourceContractError("TranscriptObserver.parse_record must be callable")
        parsed = parser(line, index, clip_text=clip_text)
        if not isinstance(parsed, ParsedRecord):
            raise SourceContractError("TranscriptObserver.parse_record must return ParsedRecord")
        return parsed

    @staticmethod
    def _decorate_parsed(parsed: ParsedRecord, source_offset: int) -> ParsedRecord:
        return ParsedRecord(
            events=tuple(replace(event, source_offset=source_offset) for event in parsed.events),
            trajectory=tuple(
                replace(fact, source_offset=source_offset) for fact in parsed.trajectory
            ),
            trajectory_events=(
                None
                if parsed.trajectory_events is None
                else tuple(
                    replace(event, source_offset=source_offset)
                    for event in parsed.trajectory_events
                )
            ),
        )

    # ---- internals ------------------------------------------------------

    def _known_location_is_trusted(self) -> bool:
        return self._known_location is not None and is_trusted_provenance(
            self._known_location_provenance
        )

    def _path_is_trusted_pin(self, path: Path | None) -> bool:
        return (
            path is not None
            and self._known_location is not None
            and path == self._known_location
            and is_trusted_provenance(self._known_location_provenance)
        )

    def _trusted_known_location_unavailable_reason(self) -> str | None:
        return trusted_location_unavailable_reason(
            location=str(self._known_location) if self._known_location is not None else None,
            provenance=str(self._known_location_provenance),
            domain=str(self._domain_root) if self._domain_root is not None else None,
        )

    @staticmethod
    def _identity_lost_batch(reason: str) -> Batch:
        return Batch(
            waiting=True,
            error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
            error=reason,
        )

    @staticmethod
    def _source_unavailable_batch(exc: OSError) -> Batch:
        return Batch(
            waiting=True,
            error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
            error=f"transcript source is unavailable: {exc}",
        )

    async def _confirmed_missing_pin_batch(self, path: Path, reason: str) -> Batch:
        """Require consecutive pin absence, checked against a healthy root.

        On the first sighting of a missing episode, before anything else, try
        exactly one same-exact-session relocation lookup: the harness may have
        renamed this exact file out from under a trusted pin (a mid-
        conversation cwd change, for Claude) rather than having actually lost
        it, and a true rename can leave *path*'s own now-empty containing
        directory removed by the harness itself on the very first poll after
        it happens. That removal must not be mistaken for the root going
        unavailable before the lookup — which is checked against the
        harness's own transcript root, not *path*'s parent — gets a chance to
        run. Both trackers are updated *before* the lookup runs (not after),
        so a rejected candidate still reaches identity loss on the very next
        poll instead of retrying the same rejected relocation forever. An
        ambiguous or absent result — or a rejected attachment — falls through
        to the ordinary consecutive-absence handling below, exactly as before
        this lookup existed.
        """
        first_sighting = self._missing_trusted_pin_once != path
        if first_sighting:
            self._missing_trusted_pin_once = path
            self._relocation_attempted = False
        if not self._relocation_attempted:
            self._relocation_attempted = True
            replacement = await self._exact_relocation(path)
            if replacement is not None:
                attached = await self._attach(replacement)
                if attached is not None:
                    return Batch(attached=attached)
        root = self._domain_root or path.parent
        try:
            if not root.is_dir():
                raise NotADirectoryError(errno.ENOTDIR, "transcript root is unavailable", root)
        except OSError as exc:
            self._missing_trusted_pin_once = None
            self._relocation_attempted = False
            return self._source_unavailable_batch(exc)
        return Batch(waiting=True) if first_sighting else self._identity_lost_batch(reason)

    async def _exact_relocation(self, missing: Path) -> Path | None:
        """A harness-verified replacement for *missing*, or ``None``.

        Only offered when this source's own session id is itself proven
        exact (not merely a guess), so the lookup can never launder a
        heuristic id into an unearned self-heal.
        """
        if self._session_id is None or self._session_provenance is not TranscriptProvenance.EXACT:
            return None
        candidate = await asyncio.to_thread(
            self._observer.exact_relocation_candidate, session_id=self._session_id
        )
        if candidate is None or candidate == missing or not self._inside_domain(candidate):
            return None
        return candidate

    def _trusted_pin_is_being_replaced_by_guess(self, path: Path, session_id: str | None) -> bool:
        return (
            self.path is not None
            and self._path_is_trusted_pin(self.path)
            and path != self.path
            and not is_trusted_provenance(self.correlation_for(path, session_id))
        )

    async def _proven_rotation(self) -> Path | None:
        """A process-proven replacement, if this adapter can supply one."""
        if self.path is None or not self._observer.proves_ownership:
            return None
        proven = await asyncio.to_thread(self._observer.proven_transcript, cwd=self._cwd)
        if proven is None or proven == self.path or not self._inside_domain(proven):
            return None
        self._proven[proven] = TranscriptProvenance.PROVEN
        return proven

    def _detach(self) -> None:
        """Forget an accepted file that vanished; collision rejection is staged."""
        self.path = None
        self.offset = self.index = self.mtime = 0

    def _require_decision(self) -> None:
        if self._pending is not None:
            raise RuntimeError("attachment must be committed or discarded before reading again")

    def _inside_domain(self, path: Path) -> bool:
        if self._domain_root is None:
            return True
        try:
            path.resolve().relative_to(self._domain_root)
        except ValueError:
            return False
        return True

    async def _locate(self, *, session_id: str | None) -> Path | None:
        if not self._cwd:
            return None
        path = await asyncio.to_thread(
            self._observer.find_transcript,
            cwd=self._cwd,
            session_id=session_id,
            after=self._after,
        )
        if path is not None and not self._inside_domain(path):
            logger.warning(
                "transcript candidate %s is outside source domain %s", path, self._domain_root
            )
            return None
        return path

    async def _upgraded(self, pinned: Path) -> Path:
        """*pinned*, unless the observer can prove a better location.

        A location admitted earlier is only as good as the evidence that
        admitted it. A heuristic one — the newest transcript in a shared
        working directory — may be a sibling's, and a participant that was
        bound that way stays bound that way forever: every later poll takes the
        pin before discovery is ever consulted, so proof that arrives
        afterwards never gets asked for. That is precisely the participant a
        proof channel is for, so a pin that is not already exact is offered to
        it once per attempt.

        Proof only, and deliberately not `find_transcript`: a probe that fails
        must leave the pin exactly as it was. Discovery would answer with a cwd
        guess instead, which is how an admitted location drifts onto a
        sibling's file — the one outcome worse than staying heuristic.
        """
        if not self._observer.proves_ownership:
            return pinned
        if (
            normalize_provenance(self.correlation_for(pinned, self._observer.session_id(pinned)))
            is not TranscriptProvenance.HEURISTIC
        ):
            return pinned
        proven = await asyncio.to_thread(self._observer.proven_transcript, cwd=self._cwd)
        if proven is None or not self._inside_domain(proven):
            return pinned
        self._proven[proven] = TranscriptProvenance.PROVEN
        if proven == pinned:
            return pinned
        logger.info(
            "transcript %s is held open by this participant's own process; "
            "replacing the location admitted from cwd evidence (%s)",
            proven,
            pinned,
        )
        return proven

    async def _attach(self, path: Path | None = None) -> Attachment | None:
        """Stage the end of a transcript. None if there is not one yet.

        Pass `path` to propose a known file (a rotation); omit it to go looking.
        """
        if path is None:
            path = self._known_location
            if path is not None and not self._inside_domain(path):
                path = None
            if path is not None:
                try:
                    path.stat()
                except OSError as exc:
                    if exc.errno != errno.ENOENT or self._path_is_trusted_pin(path):
                        raise
                    path = None
            if path is not None:
                path = await self._upgraded(path)
            if path is None:
                path = await self._locate(session_id=self._session_id)
            if path is None:
                return None
        if not self._inside_domain(path):
            return None
        size, lines, mtime, last_line, dev, ino = await asyncio.to_thread(attach_point, path)
        session_id = self._observer.session_id(path)
        last_event: Event | None = None
        if last_line is not None:
            parsed = self._parse_record(last_line, lines - 1, clip_text=True)
            semantic = [event for event in parsed.events if not event.usage_only]
            last_event = semantic[-1] if semantic else None
        self._pending = (path, size, lines, mtime, session_id)
        return Attachment(
            location=str(path),
            session_id=session_id,
            skipped=lines,
            last_event=last_event,
            point=StreamPoint(records=lines, size=size, dev=dev, ino=ino),
            correlation=self.correlation_for(path, session_id),
            collision_domain=self.collision_domain,
        )

    def _drain(self) -> Batch:
        """Read whatever the transcript grew by.

        Runs on the event loop rather than in a thread. It is a read of the
        bytes appended since the last poll — usually none, occasionally a few
        kilobytes — and the parse is pure, so the thread hop would cost more
        than it saves.
        """
        assert self.path is not None
        path, offset, index, mtime = self.path, self.offset, self.index, self.mtime

        st = path.stat()
        size = st.st_size
        # Same-length rewrite is indistinguishable from no-op; guessing wrong corrupts.
        if size < offset or (size == offset and st.st_mtime_ns != mtime):
            logger.info("transcript %s was rewritten; re-reading from the top", path)
            offset = index = 0
        if size == offset:
            self.mtime = st.st_mtime_ns
            return Batch()

        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()
            mtime = os.fstat(fh.fileno()).st_mtime_ns
        head, sep, _tail = data.rpartition(b"\n")
        if not sep:
            # A record is still being written; partial JSON is not parseable.
            self.mtime = mtime
            return Batch()
        record_offset = offset
        offset += len(head) + 1

        events: list[Event] = []
        trajectory: list[TrajectoryFact] = []
        trajectory_events: list[Event] = []
        for raw in head.split(b"\n"):
            line = raw.decode("utf-8", errors="replace")
            parsed = self._parse_record(line, index, clip_text=True)
            decorated = self._decorate_parsed(parsed, record_offset)
            events.extend(decorated.events)
            trajectory.extend(decorated.trajectory)
            trajectory_events.extend(decorated.baseline_events)
            record_offset += len(raw) + 1
            index += 1

        progressed = offset != self.offset
        self.offset, self.index, self.mtime = offset, index, mtime
        return Batch(
            events=events,
            progressed=progressed,
            trajectory=trajectory,
            trajectory_events=trajectory_events,
        )
