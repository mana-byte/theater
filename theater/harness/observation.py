"""The observation half of a harness adapter.

A plugin answers two unrelated questions. *How do I start this CLI so it comes
up knowing its participant id* — that is `Harness`, in base.py. *How do I tell
what it is doing once it is running* — that is everything here.

Why they are separate objects
-----------------------------
They were one interface until v1.6, and the seam was visible from outside.
`OpenCodeHarness` had to implement `find_transcript`, `session_id`, `parse` and
`native_children` purely to return nothing, because its output is a shared
SQLite database and none of those questions has an answer for it. A plugin
should not have to write four stubs to say "not applicable"; that it had to
meant the interface was describing one particular way of observing rather than
observation itself.

Splitting also fixes who talks to whom. The daemon's reducer
(`theater/daemon/observer.py`) needs nothing from a harness except how to watch
it, and it now holds a `HarnessObserver` rather than a `Harness` — so the launch
path and the observe path cannot accidentally couple, and a future harness that
is launched one way and observed another does not have to pretend to be one
object.

The two halves of observing
---------------------------
Getting the facts is per-CLI and lives here. Deciding what they mean — status
policy, quiet timers, job completion, every write to the registry and the bus —
is identical for every CLI and stays in the one shared reducer. That boundary is
not negotiable: every observation bug this project has had lived in the second
half, and four copies of it would mean fixing each one four times, badly.

So a plugin observer reports; it never acts. See harness/source.py for the same
rule stated for `Source`, which is the object this hands back.

Which base class to subclass
----------------------------
`TranscriptObserver` if the CLI appends a transcript file — three of the four
shipped adapters do, and it wants three methods: where the file is, what the
session is called, and how to read one line of it. `HarnessObserver` directly if
the output lives anywhere else, in which case write a `Source` and return it
from `open_source`; `opencode.py` is the worked example.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from theater.harness.base import Event, NativeChild
from theater.harness.source import StreamPoint, TranscriptCandidate
from theater.provenance import TranscriptProvenance, normalize_provenance

if TYPE_CHECKING:
    from theater.harness.source import Source


class ScreenKind(StrEnum):
    """What the rendered screen is showing, at the level a consumer needs.

    The split that ``is_idle_screen`` could not make: a boolean answers "is
    this pane waiting for input" but cannot say *what kind* of input.
    ``approval`` and ``trust`` are the ones that carry an unrecoverable cost —
    at an approval prompt, Enter is a button press, so injecting a prompt into
    that pane can auto-approve a tool call the human never saw.
    """

    WORKING = "working"
    PROMPT = "prompt"
    APPROVAL = "approval"
    TRUST = "trust"
    UNKNOWN = "unknown"


class ScreenConfidence(StrEnum):
    """How sure the observer is about its classification.

    ``low`` is the honest default for any reading derived from a heuristic
    over a text scrape: a capture is a snapshot, not a state machine, and the
    only harnesses that can answer with certainty are ones that expose their
    own UI state out-of-band. The default shim always reports ``low``
    because the boolean it derives from was itself tuned to accept false
    negatives.
    """

    LOW = "low"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class ScreenReading:
    """A structured classification of the rendered screen.

    The replacement for the boolean ``is_idle_screen``, introduced because the
    two consumers of a screen reading need **opposite** safety properties and a
    single boolean cannot serve both:

    * The **rescue path** (``observer.py:_rescue_jobs``) must never falsely
      conclude "prompt": a false idle finishes a caller's job with a partial
      answer, which is unrecoverable.
    * The **send gate** (future, per v1.7 Phase C/F) must never falsely
      conclude "approval"/blocked: a false block makes a healthy pane
      permanently unreachable, which is also unrecoverable.

    ``unknown`` is therefore resolved differently per consumer, and this type
    does not pick one global default. A gate that protects against injection
    must treat ``unknown`` as *not* ``prompt``; the régie's display hint may
    treat ``unknown`` as ``prompt`` because the cost of being wrong there is a
    cosmetic mislabel. The consumer decides — encoding a single resolution
    here would re-create the boolean's ambiguity in a richer type.

    See ``docs/v1.6_observation.md`` (lines 81-95) and
    ``docs/v1.7_hardening.md`` (lines 223-228, 496-505) for the design history.
    """

    kind: ScreenKind
    confidence: ScreenConfidence = ScreenConfidence.LOW


class HarnessObserver(ABC):
    """How to watch one harness. One instance per harness, held by it.

    Stateless with respect to participants: this object is shared by every
    session of its harness, and anything per-session belongs on the `Source` it
    opens. Configuration that locates the harness's output — a transcript root,
    a database path — belongs here, and is what makes these injectable in tests
    without going near the user's real home directory.
    """

    #: True means `open_source` reports real turns. False means there is nothing
    #: to read and the reducer falls back to `capture-pane`, ending a turn when
    #: the prompt comes back — a much weaker signal. Not a preference: it
    #: selects which watch loop the reducer runs.
    has_transcript: bool = True

    def open_source(
        self,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Source:
        """A live view of one participant's output, for the reducer to poll.

        Called once per participant, at watcher start. The returned object owns
        the reading and may hold a file handle or a connection open for the life
        of the watcher.

        Not abstract, and it raises rather than returning something empty. An
        observer with `has_transcript = True` is promising this method works;
        one with it False is promising this method is never called. The ABC
        cannot express "abstract only when that flag is set", so the failure is
        raised at the moment the contradiction actually matters, naming both
        halves of it.
        """
        raise NotImplementedError(
            f"{type(self).__name__} sets has_transcript = "
            f"{self.has_transcript!r} but does not implement open_source"
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
        """Open a source with the Theater participant identity available.

        Most transcript formats do not record Theater's id, so the default
        deliberately forwards to :meth:`open_source` and costs existing
        third-party observers nothing. A harness with a process-local
        correlation channel can override this method without pushing that
        concern into the reducer or changing the long-standing plugin method.

        An override may accept **more** than this — ``pane_pid`` is the
        current example — and :func:`open_participant_source` will offer it.
        What an override must not do is accept less.
        """
        return self.open_source(cwd=cwd, session_id=session_id, after=after)

    @abstractmethod
    def is_idle_screen(self, capture: str) -> bool:
        """Does the rendered screen show a bare prompt (waiting for input)?

        `capture` is `tmux capture-pane -p` — the pane as plain text. Each
        harness recognises its own prompt.

        Tuned to accept false negatives (return False when unsure) and never
        false positives. A false positive marks a working agent as idle, which
        hides activity from the régie and, for a harness with no transcript,
        finishes a caller's job with a partial answer.

        Abstract even for an adapter that can read its own transcript, because
        the reducer uses this for two things reading cannot do: distinguishing
        "blocked on a permission prompt" from "thinking", and confirming a pane
        looks idle before it rescues a job whose turn end was never seen.

        Of those two promises, the rescue guard is the one this method actually
        delivers: every shipped adapter treats a bare last line as idle. The
        distinction between a permission prompt and ordinary thinking is the
        one the docstring has always claimed but that ``ScreenReading`` — not a
        boolean — is the type that can actually make. Two of the four shipped
        adapters (``claude``, ``vibe``) match a bare last line positively;
        the other two (``codex``, ``opencode``) test for the absence of a
        working marker. Neither shape can tell an approval modal from a prompt,
        and that gap is the reason ``screen_reading`` exists.
        """

    def screen_reading(self, capture: str) -> ScreenReading:
        """A structured classification of the rendered screen.

        The replacement for ``is_idle_screen``, introduced because a single
        boolean cannot separate "waiting at its input prompt" from "showing an
        approval modal" — and at an approval prompt, Enter is a button press,
        so injecting a prompt into that pane can auto-approve a tool call the
        human never saw. That is the one false positive with an unrecoverable
        cost, and it is why ``ScreenReading`` carries a ``kind`` rather than a
        boolean.

        **Not abstract.** This default implementation is a compatibility shim
        that derives a reading from the existing boolean: ``is_idle_screen``
        True maps to ``kind=prompt, confidence=low``, and False maps to
        ``kind=unknown, confidence=low``. Third-party plugins living in
        ``$THEATER_HOME/harnesses`` that only implement the boolean keep
        working unchanged — a later phase will override this method
        per-harness to return ``approval``/``trust``/``working`` with
        ``high`` confidence where the CLI exposes the information.

        Both readings from the shim carry ``confidence=low`` because the
        boolean it derives from was itself tuned to accept false negatives,
        and a heuristic over a text scrape cannot claim more than that.
        ``unknown`` rather than ``working`` is chosen for the not-idle case
        so that a send gate — which must never falsely conclude "blocked" —
        treats a low-confidence non-idle screen as "do not know" rather than
        "safe to send". See the ``ScreenReading`` docstring for why the
        consumer, not this type, resolves ``unknown``.
        """
        if self.is_idle_screen(capture):
            return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.LOW)
        return ScreenReading(kind=ScreenKind.UNKNOWN, confidence=ScreenConfidence.LOW)

    def native_children(self, transcript: Path) -> list[NativeChild]:
        """Sub-agents this session spawned by itself, outside Theater.

        The second lineage edge in the spec (§5): we did not create them, cannot
        address them, and only learn of them by reading the parent's own
        bookkeeping.

        Defaults to none, which is the honest answer for a harness that spawns
        no sub-agents and for one whose sub-agents are not reachable from a
        transcript path.
        """
        return []

    def stream_floor(self, location: str) -> StreamPoint | None:
        """Capture the current stream position of a transcript, or None.

        Called by the spawner at the last safe pre-launch moment to record
        where a dead predecessor's transcript ended, so the successor's
        observer can refuse to attribute stale pre-floor records as the
        successor's own output. The floor is a fact about the stream at a
        moment, not a permission: the reducer compares it against the
        successor's first attachment and suppresses last-event-derived
        status/completion unless the attachment is provably the same stream
        (same device/inode, non-shrunk size, strictly more records).

        Returns ``None`` by default — a source that cannot produce file facts
        has no floor to offer. The spawner encodes ``None`` as
        ``UNKNOWN_FLOOR`` so the reducer treats it as present-but-unknown
        (suppress completion) rather than cold spawn (no suppression).
        ``TranscriptObserver`` overrides this for file paths.
        """
        return None

    def transcript_candidates(
        self,
        *,
        cwd: str | None,
        domain: str | None = None,
        after: float | None = None,
    ) -> list[TranscriptCandidate]:
        """Operator recovery candidates, explicitly not participant-attributed content."""
        return []

    def validate_transcript_receipt(
        self,
        *,
        payload: Mapping[str, object],
        cwd: str | None,
        expected_session_id: str | None,
    ) -> TranscriptCandidate:
        """Validate an opaque lifecycle-hook receipt into a transcript candidate.

        ``payload`` is the decoded JSON object the harness's lifecycle hook
        sent. Core never inspects it — the plugin owns every field name,
        path rule, and record-format check. A Claude receipt carries
        ``session_id``/``sessionId`` and ``transcript_path``/``transcriptPath``;
        a different harness may carry anything at all, and core treats it
        as an opaque blob so the mechanism is generic.

        The return value must carry a non-empty ``location`` and a non-empty
        ``session_id``; core rejects a candidate that does not. Rejection is
        an exception, never a candidate carrying ``rejection_reason``: raise
        ``ValueError`` with prose telling the caller what to fix, and core
        will map it to a ``BadRequest``.

        The base implementation refuses. A plugin that wants to use
        receipts must override this method. Not abstract, because making it
        abstract would break every existing plugin that does not use
        receipts — and most do not.
        """
        raise ValueError(
            f"harness {type(self).__name__} does not implement "
            "validate_transcript_receipt; a plugin must implement this hook "
            "to use transcript receipts. See docs/harness-plugins.md"
        )

    def admit_operator_candidate(
        self,
        *,
        cwd: str | None,
        candidate: str,
        domain: str | None = None,
        after: float | None = None,
    ) -> TranscriptCandidate:
        """Validate an operator-named candidate before the daemon persists trust."""
        raise ValueError(f"{type(self).__name__} has no operator-bindable transcript")


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

    #: Cwd-only relocation is unsafe in a machine-global transcript root: a
    #: newer file may belong to a sibling. A participant-isolated observer may
    #: opt in because every candidate under its root has the same owner.
    relocate_by_cwd: bool = False

    #: Whether `proven_transcript` can ever answer with anything. Left False by
    #: the observers that have no proof to offer, so a source holding an
    #: admitted location skips the call rather than paying a thread hop to be
    #: told None on every attempt.
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
        from theater.harness.source import attach_point

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
        from theater.harness.source import TranscriptSource

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
        from theater.harness.source import TranscriptSource

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
