"""Where an observer gets its input, and what it hands back.

The observer does two jobs. It *gets* what an agent said, and it *decides* what
that means — idle or working, turn over, job finished, participant dead. The
second job is identical for every harness and is where every observation bug in
this project has been. The first job is different for every harness: vibe and
claude append JSONL, opencode writes a shared SQLite database, and a future one
may only offer an HTTP event stream.

So the first job is the seam. A `Source` produces `Batch`es; the observer owns
everything that happens to them. An adapter that writes a transcript gets
`TranscriptSource` for free by subclassing `TranscriptObserver`, which is why
three of the four shipped adapters do not mention any of this.

What a source may and may not do
--------------------------------
A source reports facts. It does not touch the registry, the bus or the job
manager — not because it could not, but because the moment two sources can, the
policy that used to live in one place lives in as many places as there are
harnesses, and the fix we shipped for one is missing from the rest.

Immutability, and why `Batch.status` exists
-------------------------------------------
Tailing an append-only file gives a strong guarantee: a byte offset is a proof
that everything before it is final. A source reading a mutable store has only a
watermark — rows behind the cursor may still change. Such a source must hold a
record back until it is terminal rather than emit something it cannot retract,
and it should report `status` directly instead of letting the observer infer
one from silence. The quiet timers exist for sources that cannot tell us; a
source that knows is believed.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from theater.harness.base import Event
from theater.models import Status
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
    from theater.harness.observation import TranscriptObserver

logger = logging.getLogger("theater.harness.source")
ReceiptAdmission = Literal["accepted", "staged"]


class SourceContractError(NotImplementedError):
    """A source returned a batch it cannot complete the protocol for."""


def attach_point(path: Path) -> tuple[int, int, int, str | None]:
    """Byte offset, record count, mtime, and last complete line at end of file.

    The mtime is taken *after* the read, from the same descriptor, so it always
    covers every byte counted here even if a writer appended mid-scan.

    The last complete line is returned so the caller can derive an initial
    status from it without replaying history onto the bus. A spawned agent
    that finishes its turn before the observer attaches would otherwise keep
    the wrong status: no new bytes arrive after attach, so nothing else fires.
    """
    size = 0
    lines = 0
    tail: list[bytes] = []
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            size += len(chunk)
            lines += chunk.count(b"\n")
            tail.append(chunk)
        mtime = os.fstat(fh.fileno()).st_mtime_ns
    last_line: str | None = None
    if lines > 0:
        data = b"".join(tail)
        head, sep, _rest = data.rpartition(b"\n")
        if sep:
            # head is everything before the last newline; the last complete line
            # is the portion after the second-to-last newline (or all of head).
            _prefix, _sep2, last_bytes = head.rpartition(b"\n")
            last_line = last_bytes.decode("utf-8", errors="replace")
    return size, lines, mtime, last_line


@dataclass(frozen=True, slots=True)
class TranscriptCandidate:
    """An operator-visible transcript candidate, not participant-attributed content."""

    location: str
    session_id: str | None = None
    mtime: float | None = None
    size: int | None = None
    provenance: str = "unattributed"
    rejection_reason: str | None = None
    domain: str | None = None


@dataclass(frozen=True, slots=True)
class Attachment:
    """A candidate input location, reported whenever a source finds one.

    Finding is not adopting. The source stages the candidate without changing
    its live cursor; the observer checks ownership and calls
    ``commit_attachment`` or ``discard_attachment`` before the next read. This
    handshake is what keeps one participant's rejected rotation from silently
    switching onto a sibling's transcript.

    `location` is whatever names the input to a human reading the bus: a file
    path today, a session id or a URL for a source that has no file. It is
    published as the `path` field of the `agent.transcript` event, which
    predates this module and is what the régie renders.

    `last_event` is the final event of the last record skipped at attach, and
    it is the reason a spawned agent that finished before we found it does not
    keep the wrong status. It is deliberately not put on the bus: attaching
    skips history rather than replaying it. Note that only the *last* event of
    that record is carried — every shipped parser puts a turn boundary on the
    final event of a record, so nothing is lost, but a parser that did
    otherwise would have its boundary missed at attach time only.
    """

    location: str
    session_id: str | None = None
    skipped: int = 0
    last_event: Event | None = None
    #: See :mod:`theater.provenance`. ``heuristic`` means cwd/time only and
    #: must not feed participant-attributed content.
    correlation: str = str(TranscriptProvenance.HEURISTIC)
    #: Two heuristic candidates can collide only when their sources search the
    #: same namespace. Vibe uses the resolved save-directory root here: a
    #: global source cannot see a participant-isolated sibling and therefore
    #: must not be blocked by it.
    collision_domain: str | None = None


@dataclass(frozen=True, slots=True)
class IdentityLossEvidence:
    """An unattributed candidate that may prove a trusted pin went stale.

    This is intentionally not an :class:`Attachment`: the observer can use it
    only as loss evidence and therefore cannot accidentally commit it as the
    participant's new transcript.

    ``session_id`` is the harness-native id the source read off the candidate,
    when it knows one.  It is populated by the adapter (which already called
    ``session_id`` on the candidate path) rather than by the observer, so the
    registry-ownership guard in the reducer can reject evidence that belongs to
    another live participant without the adapter needing to know about registry
    state.  Backward-compatible: sources that do not supply it leave it ``None``,
    and the guard treats ``None`` as "no session-id claim to check".
    """

    location: str
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class History:
    """A session's past, read back in full. What `read_transcript` asks for.

    Separate from `read()` because it answers a different question. Polling
    asks "what is new"; this asks "what was said", and the answer must be
    unclipped — the entire reason the tool exists is that the bus clips to
    `MAX_TEXT` and an agent sometimes needs the whole reply.

    It is a method on the source rather than a free function over a transcript
    path because "where the text lives" is precisely what the source owns. A
    harness with no file cannot answer the path question at all.
    """

    location: str | None = None
    events: Sequence[Event] = ()
    error_code: str | None = None
    error: str | None = None
    correlation: str = str(TranscriptProvenance.HEURISTIC)
    collision_domain: str | None = None
    #: The location came from a prior reducer-accepted attachment rather than
    #: a fresh cwd scan. A pin prevents drift but does not upgrade heuristic
    #: ownership into exact process evidence.
    pinned: bool = False


@dataclass(frozen=True, slots=True)
class Batch:
    """One poll's worth of facts from a source.

    `progressed` is not the same as "produced events", and conflating them is a
    live bug rather than a tidiness point. Both shipped harnesses write
    bookkeeping records that parse to zero events but do move the file forward.
    That is activity: if it read as silence, the 60s rescue timer would fire in
    the middle of real work and hand a caller a half-finished answer. So a
    source that consumed input says so, even when it has nothing to report.

    The converse is not required. Events imply progress, and the observer
    treats them as such, so a source that emits events without setting
    `progressed` is not punished for it.

    `waiting` means there is nothing to read *from* yet — no transcript on disk,
    no session row in the database. It is mutually exclusive with `attached`:
    finding a candidate means there is something to read from. The observer
    backs off on its search interval rather than its poll interval and runs no
    quiet timers, because silence from a source that has not attached is not
    evidence about the agent.
    """

    events: Sequence[Event] = ()
    progressed: bool = False
    status: Status | None = None
    attached: Attachment | None = None
    waiting: bool = False
    #: A persistent observation-channel failure. The reducer reports it and
    #: releases old enough jobs as crashed, while continuing to retry so a
    #: late correlation receipt can recover the watcher.
    error_code: str | None = None
    error: str | None = None


class Source(ABC):
    """A live view of one participant's output.

    Constructed per participant by `HarnessObserver.open_source` and polled by
    the reducer until the participant dies. Anything expensive to hold open — a
    file handle, a database connection, an HTTP subscription — belongs here,
    which is the whole reason this is an object and not another method on
    `Harness`.
    """

    #: Namespace searched by heuristic discovery. ``None`` is the conservative
    #: default: same-harness/same-cwd sources with no sharper declaration are
    #: treated as competitors.
    collision_domain: str | None = None

    @abstractmethod
    async def read(self) -> Batch:
        """Whatever has happened since the last call. Never raises for an
        input that is merely absent — that is `Batch(waiting=True)`."""

    async def refresh(self) -> Batch:
        """Re-check where the input lives, after a stretch of silence.

        Called by the observer on the relocate timer rather than every poll,
        because for a file-backed source this is a directory scan. The default
        is to do nothing: a source whose location cannot change needs no such
        check.
        """
        return Batch()

    async def probe_identity_loss(self) -> IdentityLossEvidence | None:
        """Return bounded heuristic rotation evidence, never a new binding."""
        return None

    def commit_attachment(self) -> None:
        """Adopt the attachment most recently returned by ``read``/``refresh``.

        Sources must stage a candidate rather than changing their live cursor
        before the observer has checked that another participant does not own
        it. A source that can return ``Batch(attached=...)`` must implement both
        halves of this handshake. Failing loudly here is safer than silently
        accepting an attachment whose cursor may already point at a sibling's
        transcript.
        """
        raise SourceContractError(
            f"{type(self).__name__} returned an attachment without implementing commit_attachment()"
        )

    def discard_attachment(self) -> None:
        """Forget the staged attachment without changing the live cursor."""
        raise SourceContractError(
            f"{type(self).__name__} returned an attachment without implementing "
            "discard_attachment()"
        )

    def revoke_attachment(self) -> None:
        """Drop an accepted heuristic attachment superseded by exact evidence."""
        raise SourceContractError(f"{type(self).__name__} cannot revoke an accepted attachment")

    def admit_exact_location(self, *, location: str, session_id: str) -> ReceiptAdmission:
        """Move discovery to a daemon-proven transcript location."""
        raise SourceContractError(f"{type(self).__name__} cannot admit transcript receipts")

    async def history(self, *, last_n: int) -> History:
        """The session so far, with text unclipped. Newest `last_n` events.

        Independent of the poll cursor: reading history must not disturb where
        the watcher has got to, because the caller is usually a *different*
        consumer — `read_transcript` opens its own short-lived source rather
        than borrowing the observer's.

        `last_n <= 0` means everything. The default returns nothing, which is
        the honest answer for a source that can only see forward.
        """
        return History()

    async def aclose(self) -> None:
        """Release anything held open. Called once, when the watcher stops."""
        return


class TranscriptSource(Source):
    """Tail an append-only transcript file. What `TranscriptObserver` returns.

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
        #: Updated when an accepted attach reveals the harness's own session id, so a
        #: later re-attach can use the sharper key.
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
        #: Locations proven outside cwd discovery, with their strength. Held
        #: here rather than left to the adapter, so "the observer proved it"
        #: and "the source calls it trusted" cannot come apart: an override
        #: that proves a location without also recording it somewhere would
        #: otherwise have its answer labelled a guess.
        self._proven: dict[Path, TranscriptProvenance] = {}
        self.path: Path | None = None
        self.offset = 0
        self.index = 0
        self.mtime = 0
        self._pending: tuple[Path, int, int, int, str | None] | None = None
        #: A trusted pin must be absent on two consecutive reads before the
        #: observer treats ENOENT as identity loss. This closes the brief gap
        #: exposed by atomic replacement without adding a wall-clock heuristic.
        self._missing_trusted_pin_once: Path | None = None

    async def read(self) -> Batch:
        self._require_decision()
        if self.path is None:
            if reason := self._trusted_known_location_unavailable_reason():
                assert self._known_location is not None
                return self._confirmed_missing_pin_batch(self._known_location, reason)
            try:
                attached = await self._attach()
            except OSError as exc:
                if self._known_location_is_trusted() and exc.errno == errno.ENOENT:
                    assert self._known_location is not None
                    return self._confirmed_missing_pin_batch(
                        self._known_location,
                        f"trusted transcript pin {str(self._known_location)!r} "
                        "no longer exists on disk",
                    )
                self._missing_trusted_pin_once = None
                return self._source_unavailable_batch(exc)
            self._missing_trusted_pin_once = None
            return Batch(attached=attached) if attached else Batch(waiting=True)
        try:
            batch = self._drain()
        except OSError as exc:
            if self._path_is_trusted_pin(self.path) and exc.errno == errno.ENOENT:
                return self._confirmed_missing_pin_batch(
                    self.path,
                    f"trusted transcript pin {str(self.path)!r} no longer exists on disk",
                )
            if exc.errno == errno.ENOENT:
                # Heuristic transcript deleted or rotated; drop back to searching.
                self._missing_trusted_pin_once = None
                self._known_location = None
                self._detach()
                return Batch(waiting=True)
            self._missing_trusted_pin_once = None
            return self._source_unavailable_batch(exc)
        else:
            self._missing_trusted_pin_once = None
            return batch

    async def refresh(self) -> Batch:
        """Propose the newest transcript if the harness started a new one.

        Located by cwd alone, ignoring the session id: vibe opens a new session
        directory every turn, and the id we stored pins `find_transcript` to
        the first one, which never grows again.

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
            # Heuristic movement is never an attachment. The separate bounded
            # probe may report it as loss evidence, but it cannot auto-repoint.
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
                # The id is about to be replaced by one read off a file we only
                # guessed at. Leaving an exact session claim set would let the next
                # question about this location answer "exact" — the id matches,
                # because it was just copied from there — which launders the
                # guess into proof and can outrank real evidence later.
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
        # The id came from the revoked file. Retaining it would make discovery
        # select the same foreign transcript again instead of returning to the
        # participant's own cwd/time evidence.
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
        return History(
            location=str(path),
            events=events[-last_n:] if last_n > 0 else events,
            correlation=self.correlation_for(path, self._observer.session_id(path)),
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
            with path.open(encoding="utf-8", errors="replace") as fh:
                for index, raw in enumerate(fh):
                    line = raw.strip()
                    if line:
                        events.extend(self._observer.parse(line, index, clip_text=False))
        except OSError:
            if strict:
                raise
            # A transcript that vanished mid-read is the same non-event here.
            return []
        return events

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

    def _confirmed_missing_pin_batch(self, path: Path, reason: str) -> Batch:
        """Require consecutive pin absence while its containing root is healthy."""
        root = self._domain_root or path.parent
        try:
            if not root.is_dir():
                raise NotADirectoryError(errno.ENOTDIR, "transcript root is unavailable", root)
        except OSError as exc:
            self._missing_trusted_pin_once = None
            return self._source_unavailable_batch(exc)
        if self._missing_trusted_pin_once == path:
            return self._identity_lost_batch(reason)
        self._missing_trusted_pin_once = path
        return Batch(waiting=True)

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
        size, lines, mtime, last_line = await asyncio.to_thread(attach_point, path)
        session_id = self._observer.session_id(path)
        last_event: Event | None = None
        if last_line is not None:
            parsed = self._observer.parse(last_line, lines - 1)
            last_event = parsed[-1] if parsed else None
        self._pending = (path, size, lines, mtime, session_id)
        return Attachment(
            location=str(path),
            session_id=session_id,
            skipped=lines,
            last_event=last_event,
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
        # Size alone cannot tell "nothing happened" from "rewritten to the same
        # length", and guessing wrong is not a missed event but a corrupt one:
        # the offset lands mid-record and every later parse is garbage.
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
            # A record is still being written. Partial JSON is not parseable, and
            # buffering it here would duplicate what the file already does.
            self.mtime = mtime
            return Batch()
        offset += len(head) + 1

        events: list[Event] = []
        for raw in head.split(b"\n"):
            line = raw.decode("utf-8", errors="replace")
            events.extend(self._observer.parse(line, index))
            index += 1

        progressed = offset != self.offset
        self.offset, self.index, self.mtime = offset, index, mtime
        return Batch(events=events, progressed=progressed)
