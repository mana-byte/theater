"""Source contract: how an observer gets its input and what it hands back.

The observer does two jobs. It *gets* what an agent said, and it *decides* what
that means — idle or working, turn over, job finished, participant dead. The
second job is identical for every harness and is where every observation bug in
this project has been. The first job varies: a harness may append a transcript,
write a mutable database, or expose an event stream.

So the first job is the seam. A ``Source`` produces ``Batch``es; the observer
owns everything that happens to them. An adapter that writes a transcript gets
``TranscriptSource`` for free by subclassing ``TranscriptObserver``, which is
why file-backed adapters need little custom source machinery.

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
from dataclasses import dataclass, replace
from typing import Literal

from theater.constants.trajectory import (
    TRAJECTORY_CURSOR_MAX_BYTES,
    TRAJECTORY_PAGE_RECORD_LIMIT,
)
from theater.harness.contracts.channels import ChannelHealth
from theater.harness.contracts.events import Event
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.models import Status
from theater.provenance import TranscriptProvenance
from theater.trajectory.content import ContentPreview

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
    #: Heuristic collision namespace used to isolate competing source domains.
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
    """A legacy full-history projection for internal consumers.

    Separate from ``read()`` because it answers a different question. Polling
    asks "what is new"; this asks "what was said", and the answer must be
    unclipped. Bounded agent-facing transcript reads use ``HistoryPage``.

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
    complete_events: Sequence[Event] | None = None
    trajectory: Sequence[TrajectoryFact] = ()
    #: None projects all control events into trajectory records.
    trajectory_events: Sequence[Event] | None = None
    cursor: str | None = None
    #: Source-owned boundary that replays this page without advancing the live tail.
    snapshot_cursor: str | None = None
    older_cursor: str | None = None
    has_older: bool = False
    error_code: str | None = None
    error: str | None = None
    provenance: str = str(TranscriptProvenance.HEURISTIC)
    collision_domain: str | None = None
    pinned: bool = False

    def __post_init__(self) -> None:
        events = tuple(self.events)
        complete_events = None if self.complete_events is None else tuple(self.complete_events)
        trajectory = tuple(self.trajectory)
        trajectory_events = (
            None if self.trajectory_events is None else tuple(self.trajectory_events)
        )
        if any(not isinstance(event, Event) for event in events):
            raise SourceContractError("history page events must contain Event values")
        if complete_events is not None and any(
            not isinstance(event, Event) for event in complete_events
        ):
            raise SourceContractError("history page complete_events must contain Event values")
        if any(not isinstance(fact, TrajectoryFact) for fact in trajectory):
            raise SourceContractError("history page trajectory must contain TrajectoryFact values")
        if trajectory_events is not None and any(
            not isinstance(event, Event) for event in trajectory_events
        ):
            raise SourceContractError("history page trajectory_events must contain Event values")
        if len(events) > TRAJECTORY_PAGE_RECORD_LIMIT:
            raise SourceContractError("history page events exceed the page record limit")
        if len(trajectory) > TRAJECTORY_PAGE_RECORD_LIMIT:
            raise SourceContractError("history page trajectory exceeds the page record limit")
        if trajectory_events is not None and len(trajectory_events) > TRAJECTORY_PAGE_RECORD_LIMIT:
            raise SourceContractError("history page trajectory_events exceed the page record limit")
        object.__setattr__(
            self,
            "events",
            tuple(bound_history_event(event) for event in events),
        )
        object.__setattr__(self, "complete_events", complete_events)
        object.__setattr__(self, "trajectory", trajectory)
        object.__setattr__(self, "trajectory_events", trajectory_events)
        for name in ("cursor", "snapshot_cursor", "older_cursor"):
            value = getattr(self, name)
            if value is None:
                continue
            try:
                encoded_length = len(value.encode("utf-8")) if isinstance(value, str) else 0
            except UnicodeEncodeError as exc:
                raise SourceContractError(f"history page {name} is not valid UTF-8") from exc
            if (
                not isinstance(value, str)
                or not value
                or encoded_length > TRAJECTORY_CURSOR_MAX_BYTES
            ):
                raise SourceContractError(f"history page {name} exceeds identifier bounds")
        if type(self.has_older) is not bool or type(self.pinned) is not bool:
            raise SourceContractError("history page booleans must be booleans")

    @property
    def facts(self) -> tuple[TrajectoryFact, ...]:
        return tuple(self.trajectory)

    @property
    def transcript_events(self) -> tuple[Event, ...]:
        return tuple(self.events if self.complete_events is None else self.complete_events)

    @property
    def correlation(self) -> str:
        return self.provenance


def bound_history_event(event: Event) -> Event:
    raw_text = ContentPreview.from_text(event.raw_text).text if event.raw_text is not None else None
    return replace(event, text=ContentPreview.from_text(event.text).text, raw_text=raw_text)


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
    #: None projects all control events into trajectory records.
    trajectory_events: Sequence[Event] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "trajectory", tuple(self.trajectory))
        if self.trajectory_events is not None:
            object.__setattr__(self, "trajectory_events", tuple(self.trajectory_events))
        if any(not isinstance(fact, TrajectoryFact) for fact in self.trajectory):
            raise SourceContractError("batch trajectory must contain TrajectoryFact values")
        if self.trajectory_events is not None and any(
            not isinstance(event, Event) for event in self.trajectory_events
        ):
            raise SourceContractError("batch trajectory_events must contain Event values")


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

    def health_snapshot(self) -> tuple[ChannelHealth, ...]:
        """Return immutable participant-scoped channel health."""
        return ()

    def accounting_checkpoint(self) -> str | None:
        """Return the last accounting point acknowledged as durable."""
        return None

    def pending_accounting_checkpoint(self) -> str | None:
        """Return the current batch's point, ready to persist after reduction succeeds."""
        return None

    def acknowledge_accounting_checkpoint(self) -> None:
        """Mark the current batch's accounting point as durably persisted."""
        return

    def rollback_accounting_checkpoint(self) -> None:
        """Rewind an unacknowledged accounting batch after reduction or persistence fails."""
        return

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
        """Return a legacy unclipped history projection.

        Independent of the poll cursor: reading history must not disturb where
        the watcher has got to, because the caller is usually a *different*
        consumer and opens its own short-lived source.

        ``last_n <= 0`` retains the legacy full-history behavior. The default
        returns nothing, which is the honest answer for a source that can only
        see forward. Agent-facing transcript reads use ``history_page``.
        """
        return History()

    async def history_page(
        self,
        *,
        before: str | None = None,
        snapshot: str | None = None,
        limit: int = TRAJECTORY_PAGE_RECORD_LIMIT,
        include_full_text: bool = False,
    ) -> HistoryPage:
        """Read a bounded baseline page without changing the live watcher cursor.

        ``snapshot`` replays a source-owned newest boundary for transcript chunks.
        ``include_full_text`` is reserved for bounded transcript paging.
        """
        if type(limit) is not int or limit <= 0:
            return HistoryPage(
                error_code="invalid_limit", error="history page limit must be positive"
            )
        limit = min(limit, TRAJECTORY_PAGE_RECORD_LIMIT)
        if before is not None and snapshot is not None:
            return HistoryPage(
                error_code="history_cursor_invalid",
                error="history page accepts either an older cursor or a snapshot cursor",
            )
        if before is not None or snapshot is not None:
            return HistoryPage(
                error_code="history_paging_unavailable",
                error="this source provides a bounded newest page but cannot page older history",
            )
        history = await self.history(last_n=limit)
        complete_events = tuple(history.events[:limit])
        events = tuple(bound_history_event(event) for event in complete_events)
        return HistoryPage(
            location=history.location,
            events=events,
            complete_events=complete_events if include_full_text else None,
            snapshot_cursor=None,
            error_code=history.error_code,
            error=history.error,
            provenance=history.correlation,
            collision_domain=history.collision_domain,
            pinned=history.pinned,
        )

    async def aclose(self) -> None:
        """Release anything held open. Called once, when the watcher stops."""
        return
