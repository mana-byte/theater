"""Event types shared between every harness adapter and the reducer.

Everything above this module sees only ``Event``, which is why adding a
harness means adding a file and nothing else.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from theater.constants.harness import HARNESS_EVENT_TEXT_MAX_CHARS
from theater.models import Status
from theater.trajectory.enums import CostProvenance


def clip(text: str | None) -> str:
    if not text:
        return ""
    if len(text) <= HARNESS_EVENT_TEXT_MAX_CHARS:
        return text
    return (
        text[:HARNESS_EVENT_TEXT_MAX_CHARS]
        + f"… (+{len(text) - HARNESS_EVENT_TEXT_MAX_CHARS} chars)"
    )


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

    ``recall`` records which files each job touched; the source is the
    harness's own transcript. The observer accumulates these into the
    per-job set that becomes ``touch`` rows at job end.
    """

    #: ALWAYS repo-relative — an absolute path leaks the home dir into SQLite.
    path: str
    #: Whether the file was read or written; read-then-write yields two.
    mode: Literal["read", "write"]


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Normalized, non-overlapping token counts for one model response."""

    model: str | None = None
    provider: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    reasoning_output_tokens: int = 0
    cost_usd: float | None = None
    cost_provenance: CostProvenance = CostProvenance.UNKNOWN
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class Event:
    """One thing that happened inside an agent, stripped of harness dialect."""

    kind: EventKind
    #: Text suitable for the bus: clipped to MAX_TEXT by live parsers.
    text: str = ""
    tool_name: str | None = None
    #: Wall clock from the source; the observer stamps one when absent.
    ts: float | None = None
    #: Computed during parse() — a property of the raw record, not a separate is_turn_end.
    turn_end: bool = False
    #: Harness's own turn name. None means "no claim", never "a different turn".
    turn_id: str | None = None
    #: Source record index; several events may share one.
    raw_index: int = 0
    #: Files touched; empty tuple is honest until the plugin reports paths.
    paths: tuple[EventPath, ...] = ()
    #: Same text before clipping; None means "no separate raw text", fall back to text.
    raw_text: str | None = None
    #: Per-turn token usage, when available.
    usage: TokenUsage | None = None
    #: Byte offset of the source record when the source can provide one.
    source_offset: int | None = None

    @property
    def usage_only(self) -> bool:
        """Whether this event carries accounting data only."""
        return (
            self.usage is not None
            and not self.text
            and not self.raw_text
            and self.tool_name is None
            and not self.paths
            and not self.turn_end
        )


def status_after(event: Event) -> Status:
    """The status implied by having just seen this event.

    Only two outcomes are derivable from a transcript. See the module
    docstring for why AWAITING_INPUT is not among them.
    """
    return Status.IDLE if event.turn_end else Status.WORKING
