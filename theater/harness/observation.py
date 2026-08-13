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

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from theater.harness.base import Event, NativeChild

if TYPE_CHECKING:
    from theater.harness.source import Source


class HarnessObserver(ABC):
    """How to watch one harness. One instance per harness, held by it.

    Stateless with respect to participants: this object is shared by every
    session of its harness, and anything per-session belongs on the `Source` it
    opens. Configuration that locates the harness's output — a transcript root,
    a database path — belongs here, and is what makes these injectable in tests
    without going near the user's real home directory.
    """

    #: Whether this harness can be observed by *reading* rather than by looking
    #: at the rendered screen. True means `open_source` returns something that
    #: reports real turns. False means there is nothing to read at all and the
    #: reducer falls back to `capture-pane`, ending a turn when the prompt comes
    #: back — a much weaker signal, and the only reason it exists is that some
    #: CLIs keep no history anywhere.
    #:
    #: Not a preference. It is a statement about what this adapter can do, and
    #: it selects which watch loop the reducer runs.
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
        """

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


class TranscriptObserver(HarnessObserver):
    """The default: tail an append-only transcript the harness already writes.

    Three questions and no more — where the file is, what the session is called,
    and how to turn one line into events. Everything else about tailing (byte
    offsets, torn lines, rotation, attaching at EOF) is `TranscriptSource`, and
    a plugin never sees it.
    """

    def open_source(
        self,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Source:
        from theater.harness.source import TranscriptSource

        return TranscriptSource(self, cwd=cwd, session_id=session_id, after=after)

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
