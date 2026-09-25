"""Source contract: how an observer gets its input and what it hands back.

Sources report facts and never touch registry, bus, or jobs, so policy stays in one place.
Mutable stores hold records back until terminal and report ``status`` directly.
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
from theater.harness.contracts.runtime import NativeTurnOutcome
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
    """Where a transcript stream was at a moment in time (a resume floor).

    Files use ``dev``/``ino``/``size``/``records``, mutable sources ``stream_id``/``position``;
    missing facts mean unknown, and mixing regimes fails closed (theater.resume_floor).
    """

    records: int | None = None
    size: int | None = None
    dev: int | None = None
    ino: int | None = None
    #: Opaque logical-stream identity for mutable stores; ``None`` for file points.
    stream_id: str | None = None
    #: Monotone watermark within ``stream_id``; ``None`` for file points.
    position: int | None = None


@dataclass(frozen=True, slots=True)
class Attachment:
    """A candidate input location; finding is not adopting (observer commits after checks).

    Files are resolved; non-file ones must be ``scheme://``-qualified. ``last_event``/``status``
    carry the skipped final record's state without replaying history.
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
    status: Status | None = None


@dataclass(frozen=True, slots=True)
class IdentityLossEvidence:
    """An unattributed candidate that may prove a trusted pin went stale.

    Not an :class:`Attachment`, so it can never be committed; ``session_id`` lets the reducer reject
    evidence another live participant owns.
    """

    location: str
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class History:
    """A legacy full-history projection for internal consumers (unclipped; bounded reads use
    ``HistoryPage``).
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

BATCH_TERMINAL_EVIDENCE_MAX = 512


@dataclass(frozen=True, slots=True)
class Batch:
    """One poll's worth of facts from a source.

    Set ``progressed`` for zero-event bookkeeping, or the rescue timer fires mid-work. ``waiting``
    (nothing to read yet) excludes ``attached`` and suppresses quiet timers.
    """

    events: Sequence[Event] = ()
    progressed: bool = False
    #: More complete input is ready after a cooperative yield.
    has_more: bool = False
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
    #: Optional native terminal evidence, default-empty; exact session/turn
    #: identity that can complete a Theater job once. Legacy durable sources
    #: never populate it, and a live channel is never a transcript surrogate.
    terminal_evidence: Sequence[NativeTurnOutcome] = ()

    def __post_init__(self) -> None:
        if type(self.has_more) is not bool:
            raise SourceContractError("batch has_more must be a boolean")
        object.__setattr__(self, "trajectory", tuple(self.trajectory))
        if self.trajectory_events is not None:
            object.__setattr__(self, "trajectory_events", tuple(self.trajectory_events))
        if any(not isinstance(fact, TrajectoryFact) for fact in self.trajectory):
            raise SourceContractError("batch trajectory must contain TrajectoryFact values")
        if self.trajectory_events is not None and any(
            not isinstance(event, Event) for event in self.trajectory_events
        ):
            raise SourceContractError("batch trajectory_events must contain Event values")
        object.__setattr__(self, "terminal_evidence", tuple(self.terminal_evidence))
        if any(not isinstance(outcome, NativeTurnOutcome) for outcome in self.terminal_evidence):
            raise SourceContractError(
                "batch terminal_evidence must contain NativeTurnOutcome values"
            )
        if len(self.terminal_evidence) > BATCH_TERMINAL_EVIDENCE_MAX:
            raise SourceContractError(
                "batch terminal_evidence exceeds the bound of "
                f"{BATCH_TERMINAL_EVIDENCE_MAX} outcomes"
            )


class Source(ABC):
    """A live view of one participant's output; owns anything expensive to hold open."""

    #: Namespace searched by heuristic discovery; ``None`` means competitors.
    collision_domain: str | None = None

    @abstractmethod
    async def read(self) -> Batch:
        """Whatever has happened since the last call. Never raises for an
        input that is merely absent — that is ``Batch(waiting=True)``."""

    def validate_enrichment_batch(self, batch: Batch) -> Batch:
        """Revalidate the latest read after sibling enrichments complete, without another read."""
        return batch

    async def refresh(self) -> Batch:
        """Re-check where the input lives, after silence; on the relocate timer since it may scan a
        directory.
        """
        return Batch()

    async def probe_identity_loss(self) -> IdentityLossEvidence | None:
        """Return bounded heuristic rotation evidence, never a new binding."""
        return None

    def health_snapshot(self) -> tuple[ChannelHealth, ...]:
        """Return immutable participant-scoped channel health."""
        return ()

    def source_checkpoint(self) -> str | None:
        """Return the last source point acknowledged as durable."""
        return None

    def pending_source_checkpoint(self) -> str | None:
        """Return the current batch's point after reduction succeeds."""
        return None

    def acknowledge_source_checkpoint(self) -> None:
        """Mark the current source point as durable."""
        return

    def rollback_source_checkpoint(self) -> None:
        """Rewind an unacknowledged source batch after reduction fails."""
        return

    def terminal_evidence_snapshot(self) -> tuple[NativeTurnOutcome, ...]:
        """Return consumed terminal evidence still awaiting durable delivery, for cancelled composed
        reads.
        """
        return ()

    def buffered_terminal_evidence(self) -> tuple[NativeTurnOutcome, ...]:
        """Snapshot unread outcomes (at most 512), preserved after runtime close for recovery."""
        return ()

    def commit_attachment(self) -> None:
        """Adopt the attachment most recently returned by ``read``/``refresh``.

        Must be implemented by any source returning attachments; failing loudly beats landing on a
        sibling's transcript.
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

        Independent of the poll cursor, since callers open their own short-lived source.
        ``last_n <= 0`` is full history; agent-facing reads use ``history_page``.
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
