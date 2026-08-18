"""What every harness must expose, and the normalized types it speaks in.

Two jobs live behind a harness adapter, and they are two objects:

  launching    how to start the harness so it comes up already knowing its
               participant id (see the identity note below) — `Harness`, here;
  observing    how to find what it wrote and turn it into harness-independent
               Events — `HarnessObserver`, in harness/observation.py, reachable
               as `harness.observer`.

They were one interface until v1.6. See observation.py for why they are not any
more; the short version is that an adapter whose output is not a transcript had
to implement four transcript methods in order to return nothing from all four.

The second job is the one that makes Theater cross-harness. Everything above
this module — the reducer, the bus, the TUI — only ever sees `Event`, so adding
a harness means adding a file here and nothing else.

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
needs `capture-pane` against the rendered screen, which is why
`HarnessObserver.is_idle_screen` exists alongside the reading path.
"""

from __future__ import annotations

import shutil
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from theater.models import Status

if TYPE_CHECKING:
    from theater.harness.observation import HarnessObserver

#: Name the theater MCP server is registered under inside each harness.
SERVER_NAME = "theater"

#: How long a harness should let a single theater MCP call run before giving up.
#: A shorter per-tool timeout kills the call on the wire while the daemon is
#: still waiting, so the agent sees a transport error and falls back to polling.
#: 340 = `daemon.methods.MAX_AWAIT` (300s) + the 40s slack theater's own client
#: adds around a blocking call. Duplicated rather than imported — daemon imports
#: harness, not the reverse — so move this if that ceiling moves.
MCP_TOOL_TIMEOUT = 340.0


def theater_binary() -> str:
    """Resolve the absolute path to the ``theater`` executable.

    A spawned tmux window does not inherit the daemon's PATH — tmux starts
    the window from the session's default environment, not the daemon's —
    so the bare name ``"theater"`` would not be found by the harness's MCP
    client when theater was installed via ``uv run`` / a venv. Resolve to
    an absolute path: first check PATH (covers ``uv tool install``), then
    fall back to the bin directory next to ``sys.executable`` (the venv
    case). Returns the bare name as a last resort so the failure is loud
    and diagnosable rather than silent.
    """
    found = shutil.which("theater")
    if found:
        return found
    candidate = Path(sys.executable).parent / "theater"
    if candidate.exists():
        return str(candidate)
    return "theater"


#: No default anywhere: the caller must choose, because the choice is the whole
#: safety story for a child that nobody is watching.
APPROVALS = ("manual", "edits", "yolo")

#: The bus is an activity feed, not an archive. A single tool result is
#: routinely 25 KB; the transcript on disk remains the full record.
MAX_TEXT = 2000


def clip(text: str | None) -> str:
    if not text:
        return ""
    if len(text) <= MAX_TEXT:
        return text
    return text[:MAX_TEXT] + f"… (+{len(text) - MAX_TEXT} chars)"


def whole(text: str | None) -> str:
    """The text as written. What `read_transcript` asks for."""
    return text or ""


def clipper(clip_text: bool) -> Callable[[str | None], str]:
    """Pick the text treatment a parse pass should apply.

    Both parsers need this and both used to redefine it inline, once per
    entry point. The choice is not a detail of either harness: it is whether
    the caller is filling the bus (clip) or reading a transcript back in full.
    """
    return clip if clip_text else whole


def last_screen_line(capture: str) -> str:
    """The bottom-most line with anything on it, stripped.

    Empty string for a blank pane, which no harness should count as a prompt:
    an empty capture means the pane has not drawn yet, not that it is waiting.
    """
    lines = [line for line in capture.splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


class EventKind(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class EventPath:
    """One file an event touched, as the harness reported it.

    `recall` records which files each job touched, and the source of that
    information is the harness's own transcript. An event that reads or writes
    a file carries one of these for each path it names; the observer
    accumulates them across events into the per-job set that becomes `touch`
    rows at job end.
    """

    #: ALWAYS repo-relative, never absolute. An absolute path leaks the
    #: developer's home directory into a SQLite index that reaches agent prompts.
    path: str
    #: Whether the file was read or written. A read-then-write produces two
    #: EventPaths with the same path — the honest record.
    mode: Literal["read", "write"]


@dataclass(frozen=True, slots=True)
class Event:
    """One thing that happened inside an agent, stripped of harness dialect."""

    kind: EventKind
    #: Text suitable for the bus: clipped to ``MAX_TEXT`` by live parsers.
    text: str = ""
    tool_name: str | None = None
    #: Wall clock from the transcript. Vibe writes none, so the observer stamps
    #: its own observation time instead — do not paper over the difference.
    ts: float | None = None
    #: True when the agent stopped and is waiting. Computed during parse()
    #: because it is a property of the raw record — hoisting it into a separate
    #: is_turn_end(event) would force Event to carry harness specifics.
    turn_end: bool = False
    #: The harness's own name for the turn, when it publishes one. None means
    #: "no claim", never "a different turn": two boundaries with no id are always
    #: two turns. A dedup that misses answers with the right text; one that fires
    #: wrongly swallows a real reply and leaves the caller waiting for rescue.
    turn_id: str | None = None
    #: Index of the source record in the transcript. Several events can share
    #: one index: a Vibe assistant turn with three tool calls is four events.
    raw_index: int = 0
    #: Files this event touched. The observer accumulates them across events into
    #: the per-job set that becomes `touch` rows. An empty tuple is the honest
    #: answer until the plugin is updated to report paths.
    paths: tuple[EventPath, ...] = ()
    #: The same text before clipping, when the adapter has textual content.
    #: ``None`` means "no separate raw text"; callers fall back to ``text``.
    raw_text: str | None = None


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
    #: Files containing launch secrets. Written mode 0600 by the daemon.
    private_files: dict[Path, str] = field(default_factory=dict)
    #: Exact native session id known before launch. Persisted before tmux starts
    #: so an observer never has to guess from cwd during the creation race.
    session_id: str | None = None
    #: Unguessable token accepted only for this participant's launch receipts.
    receipt_token: str | None = None
    #: Resolved namespace searched by heuristic transcript discovery, when a
    #: harness has more than one. Persisted before launch so collision policy
    #: does not depend on watcher scheduling.
    transcript_domain: str | None = None


class Harness(ABC):
    #: Key used on the wire and in `theater spawn <name>`.
    name: str
    #: Executable to look for on PATH.
    binary: str
    #: Binary basenames this harness can be recognised under, including
    #: wrapper-renamed variants (e.g. nixpkgs ``makeWrapper`` produces
    #: ``.claude-wrapped``). The primary ``binary`` is ALWAYS included
    #: regardless of what this set contains; this is for additional names only.
    #: Per AGENTS.md, per-harness knowledge belongs in the plugin, not the daemon.
    binaries: frozenset[str] = frozenset()
    #: A single glyph: terminal image protocols do not survive tmux. Kept to
    #: width 1 so no listing reflows, and from symbols a default font has rather
    #: than a Nerd Font private-use codepoint, which renders as a blank box.
    icon: str = "·"
    #: Other spellings that resolve to `name` at registration. An agent that
    #: reports a name that does not normalize is observed as nothing at all.
    aliases: tuple[str, ...] = ()
    #: Set in `__init__`, because an observer is constructed with the paths that
    #: locate the harness's output — exactly what tests inject. An annotation
    #: rather than an abstract property: `plugins._check_observer` rejects a
    #: plugin that omits it.
    observer: HarnessObserver
    #: A class attribute, not signature introspection: a signature cannot express
    #: "accepts resume but silently drops the prompt" — opencode's `-s` routes to
    #: the session view, and `--prompt` is only read on the home screen.
    resume_takes_prompt: bool = True

    # ---- launching ------------------------------------------------------

    @abstractmethod
    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
        model: str | None = None,
    ) -> LaunchPlan:
        """Describe how to start this harness. Pure: writes nothing itself.

        `model` is opaque and optional. Theater never validates it: vendor
        model namespaces change faster than any allowlist here could, so the
        string is passed through to whatever lever the CLI offers — a flag for
        most, an environment variable for one — and the harness decides. A
        plugin that cannot select a model simply omits the parameter, and
        `harness.plan_launch` rejects the request before reaching it rather
        than dropping the caller's choice on the floor. That omission is the
        compatibility story: an adapter written before this parameter existed
        keeps working for every launch that does not name a model.

        `reasoning_effort` follows the same pattern as `model` but is not in
        this abstract signature — it is added per-adapter, and the funnel
        forwards it only to adapters whose `plan_launch` accepts it. A plugin
        that cannot select a reasoning effort simply omits the parameter.
        """

    def discover_models(self) -> list[str]:
        """Model names this CLI reports it can run, for `theater models`.

        Optional, and concrete rather than abstract so that not implementing it
        costs a plugin nothing. Two of the four shipped adapters cannot answer:
        `claude` and `codex` offer no listing of any kind, and guessing on their
        behalf would produce a catalogue that goes stale silently.

        This is an authoring aid, never a gate. What a spawn may use is the
        `[models]` allowlist in Theater's own config, which the user writes; the
        job here is only to save them typing it. Whatever is returned is
        therefore a suggestion — it may be out of date, may list models the
        user is not authenticated for, and is not consulted at spawn time.

        Raise `NotImplementedError` when the CLI has no way to be asked. Return
        an empty list only for the genuinely different case of having asked and
        been told none, which is what an unauthenticated provider looks like:
        the caller reports those two states differently.
        """
        raise NotImplementedError(
            f"{self.name} cannot list its models: no command or config to read"
        )


def status_after(event: Event) -> Status:
    """The status implied by having just seen this event.

    Only two outcomes are derivable from a transcript. See the module
    docstring for why AWAITING_INPUT is not among them.
    """
    return Status.IDLE if event.turn_end else Status.WORKING
