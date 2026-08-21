"""Transcript observer mechanics: compatibility dispatch and the default adapter.

The contract types (``ScreenKind``, ``ScreenConfidence``, ``ScreenReading``,
``HarnessObserver``) live in ``theater.harness.contracts.observation``. This
module owns the transcript-specific mechanics that were always in
``observation.py``: the two compatibility-dispatch free functions and the
``TranscriptObserver`` base class that three of the four shipped adapters
subclass.
"""

from __future__ import annotations

import inspect
from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

from theater.harness.contracts.events import Event
from theater.harness.contracts.observation import HarnessObserver
from theater.harness.contracts.source import StreamPoint, TranscriptCandidate
from theater.provenance import TranscriptProvenance, normalize_provenance

if TYPE_CHECKING:
    from theater.harness.contracts.source import Source


def enumerate_transcript_candidates(
    observer: HarnessObserver,
    *,
    cwd: str | None,
    domain: str | None = None,
    after: float | None = None,
) -> list[TranscriptCandidate]:
    """Compatibility dispatch for operator transcript candidate enumeration."""
    accepted = inspect.signature(observer.transcript_candidates).parameters
    extra: dict[str, Any] = {}
    if "domain" in accepted:
        extra["domain"] = domain
    return observer.transcript_candidates(cwd=cwd, after=after, **extra)


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
    pane_pid: int | None = None,
) -> Source:
    """Compatibility dispatch for the optional participant-aware hook.

    Local harness plugins have historically been accepted by structural
    validation and need not inherit :class:`HarnessObserver`. Such an observer
    has no inherited ``open_source_for`` method, so fall back to its established
    ``open_source`` call shape. An observer that needs exact correlation opts in
    by defining the new hook.

    Each optional argument is offered only to an observer whose signature names
    it, one parameter at a time rather than one version at a time: the shipped
    adapters take different subsets, and a third-party plugin written against
    any past release keeps working without knowing which release it was.

    ``pane_pid`` is the participant's launch process — tmux's ``#{pane_pid}``.
    It is the correlation channel of last resort for a CLI that mints its own
    session id and shares a transcript root with its siblings: the files that
    process holds open say which transcript is its own. Only ``codex`` asks
    for it today. It is ``None`` for a participant with no pane, and for a
    dead one, whose pid the operating system is free to have reused —
    see :attr:`theater.models.Participant.live_pid`.
    """
    factory = getattr(observer, "open_source_for", None)
    if callable(factory):
        accepted = inspect.signature(factory).parameters
        extra: dict[str, Any] = {}
        provenance = normalize_provenance(session_provenance)
        if "session_provenance" in accepted:
            extra["session_provenance"] = provenance
        elif "session_exact" in accepted:
            extra["session_exact"] = provenance is TranscriptProvenance.EXACT
        if "known_location" in accepted:
            extra["known_location"] = known_location
        if "transcript_domain" in accepted:
            extra["transcript_domain"] = transcript_domain
        if "pane_pid" in accepted:
            extra["pane_pid"] = pane_pid
        return factory(
            participant_id=participant_id,
            cwd=cwd,
            session_id=session_id,
            after=after,
            **extra,
        )
    return observer.open_source(cwd=cwd, session_id=session_id, after=after)


class TranscriptObserver(HarnessObserver):
    """The default: tail an append-only transcript the harness already writes.

    Three questions and no more — where the file is, what the session is called,
    and how to turn one line into events. Everything else about tailing (byte
    offsets, torn lines, rotation, attaching at EOF) is `TranscriptSource`, and
    a plugin never sees it.
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
        """A bounded newer heuristic candidate, used only as loss evidence.

        Shared-root formats opt in explicitly. Returning a path here never
        attaches to it; :class:`TranscriptSource` deliberately exposes the
        result as non-committable identity-loss evidence.
        """
        return None

    def stream_floor(self, location: str) -> StreamPoint | None:
        """Capture the stream position of a file-backed transcript.

        Reads the file once with :func:`attach_point` and returns a
        :class:`StreamPoint` carrying the record count, byte size, and the
        device/inode from the same descriptor. Returns ``None`` when the
        location is not a readable file — an unavailable floor is represented
        as ``None`` rather than a partial fact, so the spawner persists a
        present-but-unknown floor instead of one that could be confused with
        a cold spawn.
        """
        from theater.harness.transcript.attachment import attach_point

        try:
            path = Path(location)
            size, lines, _mtime, _last_line, dev, ino = attach_point(path)
        except OSError:
            return None
        return StreamPoint(records=lines, size=size, dev=dev, ino=ino)

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

        Note what is *not* a parameter: the tmux pane. No shipped harness records
        which pane it was launched from anywhere on disk, so a pane cannot narrow
        the search. The usable keys are the working directory, the harness's own
        session id when we happen to know it, and a lower bound on start time.

        `after` is a floor on session start, used for participants we spawned and
        whose creation time we therefore know exactly. It is left None for
        adopted participants, whose transcript predates our first sight of them.
        """

    def proven_transcript(self, *, cwd: str | None) -> Path | None:
        """A location this participant can be *shown* to own, or None.

        Discovery's proof half, separated from its guessing half. Most harnesses
        have no such proof and answer None, which is why this is concrete rather
        than abstract; an adapter that can prove ownership — because the CLI
        holds its own transcript open, say — overrides it.

        The separation exists for the one caller that must not guess. A source
        holding a location admitted earlier can only improve on it with proof:
        calling `find_transcript` there would fall through to a cwd scan, and a
        scan that swapped an admitted location for a sibling's newer file is the
        exact mis-attribution the collision guard exists to catch. So this is
        allowed to answer "no better evidence" and never "here is a guess".
        """
        return None

    @abstractmethod
    def session_id(self, transcript: Path) -> str | None:
        """The harness's own id for the session this transcript belongs to.

        Recorded on the participant so that harness-native identifiers — which
        is what sub-agent bookkeeping is expressed in — can be matched back to a
        Theater participant later.
        """

    @abstractmethod
    def parse(self, line: str, index: int, *, clip_text: bool = True) -> list[Event]:
        """Turn one transcript line into zero or more events.

        Zero is normal and common: every harness writes bookkeeping records that
        mean nothing to an observer. Malformed lines yield zero too rather than
        raising — a transcript being appended to as we read it is an expected
        condition, not an error.

        `clip_text` False returns the full text instead of clipping to MAX_TEXT.
        The bus clips; `read_transcript` does not, which is the whole reason that
        tool exists.
        """
