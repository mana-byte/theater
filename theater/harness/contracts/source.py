"""Source contract: how an observer gets its input and what it hands back.

The observer does two jobs. It *gets* what an agent said, and it *decides* what
that means — idle or working, turn over, job finished, participant dead. The
second job is identical for every harness and is where every observation bug in
this project has been. The first job is different for every harness: vibe and
claude append JSONL, opencode writes a shared SQLite database, and a future one
may only offer an HTTP event stream.

So the first job is the seam. A ``Source`` produces ``Batch``es; the observer
owns everything that happens to them. An adapter that writes a transcript gets
``TranscriptSource`` for free by subclassing ``TranscriptObserver``, which is
why three of the four shipped adapters do not mention any of this.

What a source may and may not do
--------------------------------
A source reports facts. It does not touch the registry, the bus or the job
manager — not because it could not, but because the moment two sources can, the
policy that used to live in one place lives in as many places as there are
harnesses, and the fix we shipped for one is missing from the rest.

Immutability, and why ``Batch.status`` exists
---------------------------------------------
Tailing an append-only file gives a strong guarantee: a byte offset is a proof
that everything before it is final. A source reading a mutable store has only a
watermark — rows behind the cursor may still change. Such a source must hold a
record back until it is terminal rather than emit something it cannot retract,
and it should report ``status`` directly instead of letting the observer infer
one from silence. The quiet timers exist for sources that cannot tell us; a
source that knows is believed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from theater.constants.trajectory import TRAJECTORY_PAGE_RECORD_LIMIT
from theater.harness.contracts.events import Event
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.models import Status
from theater.provenance import TranscriptProvenance

ReceiptAdmission = Literal["accepted", "staged"]


class SourceContractError(NotImplementedError):
    """A source returned a batch it cannot complete the protocol for."""


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
class StreamPoint:
    """Where a transcript stream was at a moment in time.

    A backward-compatible fact type recording the position of a transcript
    file at the last safe pre-launch moment. The reducer compares a saved
    floor against an attachment's ``point`` to decide whether the stream the
    successor sees is provably the same one the predecessor left — same
    location, same device/inode, non-shrunk size, and strictly more records
    than the floor.

    ``records`` is the newline-delimited record count. ``size`` is the byte
    offset. ``dev`` and ``ino`` are the opaque identity from ``fstat`` on
    the same descriptor the bytes were read from. Any of them may be
    ``None`` when the source could not produce the fact, and a floor with
    missing facts is present-but-unknown: the reducer suppresses completion
    rather than guessing.
    """

    records: int | None = None
    size: int | None = None
    dev: int | None = None
    ino: int | None = None


@dataclass(frozen=True, slots=True)
class Attachment:
    """A candidate input location, reported whenever a source finds one.

    Finding is not adopting. The source stages the candidate without changing
    its live cursor; the observer checks ownership and calls
    ``commit_attachment`` or ``discard_attachment`` before the next read. This
    handshake is what keeps one participant's rejected rotation from silently
    switching onto a sibling's transcript.

    ``location`` is whatever names the input to a human reading the bus: a file
    path today, a session id or a URL for a source that has no file. It is
    published as the ``path`` field of the ``agent.transcript`` event, which
    predates this module and is what the régie renders.

    A file-backed ``location`` is canonicalised by the core to an absolute
    resolved path (``expanduser`` + ``resolve``) when it enters the observer,
    so ``~/t.jsonl`` and ``/Users/me/t.jsonl`` are the same transcript. A
    source that names a non-filesystem identity must scheme-qualify it
    (``scheme://...``) or it will be treated as a path and resolved.

    ``last_event`` is the final event of the last record skipped at attach, and
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
    #: Opaque stream identity at attach time; checked against a persisted resume floor.
    point: StreamPoint | None = None
    #: See :mod:`theater.provenance`; ``heuristic`` means cwd/time only.
    correlation: str = str(TranscriptProvenance.HEURISTIC)
    #: Heuristic collision namespace; vibe uses the save-dir root to isolate siblings.
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
    """A session's past, read back in full. What ``read_transcript`` asks for.

    Separate from ``read()`` because it answers a different question. Polling
    asks "what is new"; this asks "what was said", and the answer must be
    unclipped — the entire reason the tool exists is that the bus clips to
    ``MAX_TEXT`` and an agent sometimes needs the whole reply.

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
    #: From a prior accepted attachment, not a fresh cwd scan; prevents drift.
    pinned: bool = False


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """An independent bounded history read that never advances a live cursor."""

    location: str | None = None
    events: Sequence[Event] = ()
    trajectory: Sequence[TrajectoryFact] = ()
    cursor: str | None = None
    older_cursor: str | None = None
    has_older: bool = False
    error_code: str | None = None
    error: str | None = None
    provenance: str = str(TranscriptProvenance.HEURISTIC)
    pinned: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "trajectory", tuple(self.trajectory))
        if any(not isinstance(event, Event) for event in self.events):
            raise SourceContractError("history page events must contain Event values")
        if any(not isinstance(fact, TrajectoryFact) for fact in self.trajectory):
            raise SourceContractError("history page trajectory must contain TrajectoryFact values")
        if type(self.has_older) is not bool or type(self.pinned) is not bool:
            raise SourceContractError("history page booleans must be booleans")

    @property
    def facts(self) -> tuple[TrajectoryFact, ...]:
        return tuple(self.trajectory)

    @property
    def correlation(self) -> str:
        return self.provenance


TrajectoryHistoryPage = HistoryPage


@dataclass(frozen=True, slots=True)
class Batch:
    """One poll's worth of facts from a source.

    ``progressed`` is not the same as "produced events", and conflating them is a
    live bug rather than a tidiness point. Both shipped harnesses write
    bookkeeping records that parse to zero events but do move the file forward.
    That is activity: if it read as silence, the 60s rescue timer would fire in
    the middle of real work and hand a caller a half-finished answer. So a
    source that consumed input says so, even when it has nothing to report.

    The converse is not required. Events imply progress, and the observer
    treats them as such, so a source that emits events without setting
    ``progressed`` is not punished for it.

    ``waiting`` means there is nothing to read *from* yet — no transcript on disk,
    no session row in the database. It is mutually exclusive with ``attached``:
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
    #: Persistent channel failure; reducer reports it and retries for late recovery.
    error_code: str | None = None
    error: str | None = None
    #: Rich facts are additive; the reducer continues to consume only events.
    trajectory: Sequence[TrajectoryFact] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "trajectory", tuple(self.trajectory))
        if any(not isinstance(fact, TrajectoryFact) for fact in self.trajectory):
            raise SourceContractError("batch trajectory must contain TrajectoryFact values")


class Source(ABC):
    """A live view of one participant's output.

    Constructed per participant by ``HarnessObserver.open_source`` and polled by
    the reducer until the participant dies. Anything expensive to hold open — a
    file handle, a database connection, an HTTP subscription — belongs here,
    which is the whole reason this is an object and not another method on
    ``Harness``.
    """

    #: Namespace searched by heuristic discovery; ``None`` means competitors.
    collision_domain: str | None = None

    @abstractmethod
    async def read(self) -> Batch:
        """Whatever has happened since the last call. Never raises for an
        input that is merely absent — that is ``Batch(waiting=True)``."""

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
        """The session so far, with text unclipped. Newest ``last_n`` events.

        Independent of the poll cursor: reading history must not disturb where
        the watcher has got to, because the caller is usually a *different*
        consumer — ``read_transcript`` opens its own short-lived source rather
        than borrowing the observer's.

        ``last_n <= 0`` means everything. The default returns nothing, which is
        the honest answer for a source that can only see forward.
        """
        return History()

    async def history_page(
        self,
        *,
        before: str | None = None,
        limit: int = TRAJECTORY_PAGE_RECORD_LIMIT,
    ) -> HistoryPage:
        """Read a bounded baseline page without changing the live watcher cursor.

        The base contract can expose only a newest-page fallback. It therefore
        never invents an older cursor and reports paging as unavailable when a
        caller asks for one.
        """
        if type(limit) is not int or limit <= 0:
            return HistoryPage(
                error_code="invalid_limit", error="history page limit must be positive"
            )
        limit = min(limit, TRAJECTORY_PAGE_RECORD_LIMIT)
        if before is not None:
            return HistoryPage(
                error_code="history_paging_unavailable",
                error="this source provides a bounded newest page but cannot page older history",
            )
        history = await self.history(last_n=limit)
        return HistoryPage(
            location=history.location,
            events=history.events,
            error_code=history.error_code,
            error=history.error,
            provenance=history.correlation,
            pinned=history.pinned,
        )

    async def aclose(self) -> None:
        """Release anything held open. Called once, when the watcher stops."""
        return
