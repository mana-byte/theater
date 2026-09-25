"""Transcript observer mechanics: compatibility dispatch and the default ``TranscriptObserver``."""

from __future__ import annotations

import inspect
from abc import abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from theater.harness.contracts.context import ParticipantObservationContext
from theater.harness.contracts.events import Event
from theater.harness.contracts.observation import HarnessObserver
from theater.harness.contracts.source import StreamPoint, TranscriptCandidate
from theater.harness.contracts.trajectory import ParsedRecord
from theater.provenance import TranscriptProvenance

if TYPE_CHECKING:
    from theater.harness.contracts.source import Source


class _SourceObserver(Protocol):
    def open_source(
        self,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Source: ...


def enumerate_transcript_candidates(
    observer: HarnessObserver,
    *,
    cwd: str | None,
    domain: str | None = None,
    after: float | None = None,
) -> list[TranscriptCandidate]:
    """Compatibility dispatch for operator transcript candidate enumeration."""
    accepted = inspect.signature(observer.transcript_candidates).parameters
    if "domain" in accepted:
        return observer.transcript_candidates(cwd=cwd, domain=domain, after=after)
    return observer.transcript_candidates(cwd=cwd, after=after)


def _open_source_legacy(observer: object, context: ParticipantObservationContext) -> Source:
    """Offer only the optional source-factory arguments an observer names."""
    factory = getattr(observer, "open_source_for", None)
    if callable(factory):
        accepted = inspect.signature(factory).parameters
        extra: dict[str, object] = {}
        if "session_provenance" in accepted:
            extra["session_provenance"] = context.session_provenance
        elif "session_exact" in accepted:
            extra["session_exact"] = context.session_provenance is TranscriptProvenance.EXACT
        if "known_location" in accepted:
            extra["known_location"] = context.known_location
        if "transcript_domain" in accepted:
            extra["transcript_domain"] = context.transcript_domain
        if "pane_pid" in accepted:
            extra["pane_pid"] = context.pane_pid
        return factory(
            participant_id=context.participant_id,
            cwd=context.cwd,
            session_id=context.session_id,
            after=context.after,
            **extra,
        )
    source_observer = cast(_SourceObserver, observer)
    return source_observer.open_source(
        cwd=context.cwd,
        session_id=context.session_id,
        after=context.after,
    )


def _explicit_context_factory(
    observer: object,
) -> Callable[[ParticipantObservationContext], Source] | None:
    candidate = getattr(observer, "open_source_context", None)
    if not callable(candidate):
        return None
    if isinstance(observer, HarnessObserver):
        implementation = getattr(type(observer), "open_source_context", None)
        instance_values = getattr(observer, "__dict__", {})
        if (
            implementation is HarnessObserver.open_source_context
            and "open_source_context" not in instance_values
        ):
            return None
    return candidate


def open_participant_source(
    observer: HarnessObserver,
    *,
    participant_id: str,
    cwd: str | None,
    session_id: str | None = None,
    after: float | None = None,
    session_provenance: str | TranscriptProvenance | None = None,
    known_location: str | None = None,
    transcript_domain: str | None = None,
    source_checkpoint: str | None = None,
    pane_pid: int | None = None,
) -> Source:
    """Compatibility dispatch for the optional participant-aware hook.

    Observers need not inherit :class:`HarnessObserver`; each optional argument is offered only if
    the signature names it. ``pane_pid`` is None without a live pane since pids get reused.
    """
    context = ParticipantObservationContext(
        participant_id=participant_id,
        cwd=cwd,
        session_id=session_id,
        after=after,
        session_provenance=session_provenance,
        known_location=known_location,
        transcript_domain=transcript_domain,
        source_checkpoint=source_checkpoint,
        pane_pid=pane_pid,
    )
    if factory := _explicit_context_factory(observer):
        return factory(context)
    return _open_source_legacy(observer, context)


class TranscriptObserver(HarnessObserver):
    """The default: tail an append-only transcript the harness already writes.

    Plugins answer where, what session, and how to parse; tailing mechanics stay in
    `TranscriptSource`.
    """

    #: Cwd-only relocation is unsafe in a shared root; a participant-isolated observer may opt in.
    relocate_by_cwd: bool = False

    #: Left False by observers with no ownership proof, so a source skips the thread hop.
    proves_ownership: bool = False

    def identity_loss_candidate(
        self,
        *,
        cwd: str | None,
        current: Path,
        current_mtime_ns: int,
        after: float | None = None,
    ) -> Path | None:
        """A bounded newer heuristic candidate, used only as non-committable loss evidence."""
        return None

    def exact_relocation_candidate(self, *, session_id: str) -> Path | None:
        """A uniquely-resolved replacement for a vanished trusted pin.

        Committable, so it must be proven to carry this exact session id; ambiguity means ``None``.
        """
        return None

    def stream_floor(self, location: str) -> StreamPoint | None:
        """Capture the stream position of a file-backed transcript.

        ``None`` when unreadable, never a partial fact, so it is not confused with a cold spawn.
        """
        from theater.harness.contracts.callbacks import StreamFloorContext
        from theater.harness.transcript.identity import file_stream_floor

        return file_stream_floor(StreamFloorContext(location=location))

    def open_source(
        self,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Source:
        from theater.harness.transcript.source import TranscriptSource

        return TranscriptSource(
            self,
            cwd=cwd,
            session_id=session_id,
            after=after,
            allow_refresh=self.relocate_by_cwd,
        )

    def open_source_for(
        self,
        *,
        participant_id: str,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
        session_provenance: str | TranscriptProvenance | None = None,
        known_location: str | None = None,
    ) -> Source:
        """Preserve persisted session-id provenance in the source claim."""
        from theater.harness.transcript.source import TranscriptSource

        return TranscriptSource(
            self,
            cwd=cwd,
            session_id=session_id,
            after=after,
            allow_refresh=self.relocate_by_cwd,
            session_provenance=session_provenance,
            known_location=known_location,
        )

    @abstractmethod
    def find_transcript(
        self,
        *,
        cwd: str,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Path | None:
        """Locate the transcript for a session, or None if it is not there yet.

        No pane parameter: no harness records its pane on disk. `after` bounds spawned sessions'
        start and is None for adopted ones, whose transcript predates us.
        """

    def proven_transcript(self, *, cwd: str | None) -> Path | None:
        """A location this participant can be *shown* to own, or None.

        Proof only, never a guess: a cwd scan could swap an admitted location for a sibling's file.
        """
        return None

    @abstractmethod
    def session_id(self, transcript: Path) -> str | None:
        """The harness's own session id, so native sub-agent bookkeeping maps to a participant."""

    @abstractmethod
    def parse(self, line: str, index: int, *, clip_text: bool = True) -> list[Event]:
        """Turn one transcript line into zero or more events.

        Bookkeeping and malformed (still being appended) lines yield zero rather than raising.
        ``clip_text=False`` keeps full text for history paging.
        """

    def parse_record(self, line: str, index: int, *, clip_text: bool = True) -> ParsedRecord:
        """Adapt the legacy parser once; richer adapters may override this seam."""
        return ParsedRecord(events=tuple(self.parse(line, index, clip_text=clip_text)))
