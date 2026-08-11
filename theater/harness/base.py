"""What every harness must expose, and the normalized types it speaks in.

Two jobs live behind this interface:

  launching    how to start the harness so it comes up already knowing its
               participant id (see the identity note below);
  observing    how to find the transcript it writes and turn each line into a
               harness-independent Event.

The second job is the one that makes Theater cross-harness. Everything above
this module — the observer, the bus, the TUI — only ever sees `Event`, so
adding a third harness means adding a file here and nothing else.

The identity problem, precisely
-------------------------------
A spawned agent must be able to tell the daemon "I am participant X" from
inside its own MCP tool calls. The obvious channel — put THEATER_ID in the
pane's environment and let it be inherited — does not work. The MCP Python SDK
does not pass the parent environment through to a stdio server: when a server
config omits `env`, the SDK substitutes `get_default_environment()`, an
allowlist of six variables (HOME, LOGNAME, PATH, SHELL, TERM, USER on posix).
See mcp/client/stdio/__init__.py:28-44,127. Anything else is dropped.

So the id has to be baked into the MCP server's *argv*, which nothing filters:

    theater mcp --id <participant-id>

Each harness needs a different lever to get that argv in place; see the
subclasses. Both harnesses take the initial prompt as a positional argument and
stay interactive, which is what makes phase 5a possible without any keystroke
injection at all.

What cannot be observed from a transcript
-----------------------------------------
Status derivation here yields IDLE or WORKING and never AWAITING_INPUT. A
permission prompt writes nothing to the transcript — it is a UI state, not a
message — and "the last record is a tool_use with no result yet" is
indistinguishable from a tool that is merely slow. Detecting a blocked agent
needs `capture-pane` against the rendered screen, which is phase 5b. Until
then a paused agent reads as WORKING, which is wrong but honest: we do not
invent a state we cannot see.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from theater.models import Status

#: Name the theater MCP server is registered under inside each harness.
SERVER_NAME = "theater"

#: Approval modes accepted by `spawn`. There is no default anywhere: the caller
#: must choose, because the choice is the whole safety story for a child that
#: nobody is watching.
APPROVALS = ("manual", "edits", "yolo")

#: Events go on the bus, and the bus is an activity feed, not an archive. A
#: single tool result is routinely 25 KB; keeping it whole would put megabytes
#: of file contents into SQLite for something the TUI renders as one line. The
#: transcript on disk remains the full record.
MAX_TEXT = 2000


def clip(text: str | None) -> str:
    if not text:
        return ""
    if len(text) <= MAX_TEXT:
        return text
    return text[:MAX_TEXT] + f"… (+{len(text) - MAX_TEXT} chars)"


class EventKind(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Event:
    """One thing that happened inside an agent, stripped of harness dialect."""

    kind: EventKind
    text: str = ""
    tool_name: str | None = None
    #: Wall clock from the transcript, when the harness records one. Vibe does
    #: not write timestamps at all, so this is None there and the observer
    #: stamps its own observation time instead. Do not paper over the
    #: difference: a stamped-on-read time is not when the event happened.
    ts: float | None = None
    #: True when the agent stopped and is waiting for a human. Computed during
    #: parse() because it is a property of the raw record — stop_reason for
    #: Claude Code, absence of tool_calls for Vibe — and hoisting it into a
    #: separate is_turn_end(event) would force Event to carry those harness
    #: specifics just to answer the question later.
    turn_end: bool = False
    #: Index of the source record in the transcript. Several events can share
    #: one index: a Vibe assistant turn with three tool calls is four events.
    raw_index: int = 0


@dataclass(frozen=True, slots=True)
class NativeChild:
    """A sub-agent the harness spawned by itself, outside Theater's knowledge.

    These are the second lineage edge in the spec (§5): Theater did not create
    them, cannot address them, and only learns of them by reading the parent's
    own bookkeeping.
    """

    session_id: str
    agent: str | None = None
    relative_path: str | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class LaunchPlan:
    """Everything tmux needs to bring a participant up."""

    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    #: Files to write before the window is created, path -> contents.
    files: dict[Path, str] = field(default_factory=dict)


class Harness(ABC):
    #: Key used on the wire and in `theater spawn <name>`.
    name: str
    #: Executable to look for on PATH.
    binary: str

    # ---- launching ------------------------------------------------------

    @abstractmethod
    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
    ) -> LaunchPlan:
        """Describe how to start this harness. Pure: writes nothing itself."""

    # ---- observing ------------------------------------------------------

    @abstractmethod
    def find_transcript(
        self,
        *,
        cwd: str,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Path | None:
        """Locate the transcript for a session, or None if it is not there yet.

        Note what is *not* a parameter: the tmux pane. Nothing in either
        harness's on-disk layout records which pane it was launched from, so a
        pane cannot narrow the search. The usable keys are the working
        directory, the harness session id when we happen to know it, and a
        lower bound on start time.

        `after` is a floor on session start, used for participants we spawned
        and whose creation time we therefore know exactly. It must be left None
        for adopted participants, whose transcript predates our first sight of
        them.
        """

    @abstractmethod
    def session_id(self, transcript: Path) -> str | None:
        """The harness's own id for the session this transcript belongs to.

        Recorded on the participant so that harness-native identifiers — which
        is what sub-agent bookkeeping is expressed in — can be matched back to
        a Theater participant later.
        """

    @abstractmethod
    def parse(self, line: str, index: int) -> list[Event]:
        """Turn one transcript line into zero or more events.

        Zero is normal and common: both harnesses write bookkeeping records
        that mean nothing to an observer. Malformed lines yield zero too rather
        than raising — a transcript being appended to as we read it is an
        expected condition, not an error.
        """

    @abstractmethod
    def native_children(self, transcript: Path) -> list[NativeChild]:
        """Sub-agents this session spawned on its own. Often empty."""


def status_after(event: Event) -> Status:
    """The status implied by having just seen this event.

    Only two outcomes are derivable from a transcript. See the module
    docstring for why AWAITING_INPUT is not among them.
    """
    return Status.IDLE if event.turn_end else Status.WORKING
