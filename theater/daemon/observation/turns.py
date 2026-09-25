"""Turn boundary accumulation and prompt matching.

Pure conversation state: what the agent said and what it was replying to.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field

from theater.constants.observation import ANSWERED_TURNS, PROMPT_MATCH


def answers_prompt(heard: Sequence[str], prompt: str | None) -> bool:
    """Did this turn begin with the prompt we injected? (normalised prefix, both ways)

    Driven harnesses echo the prompt first (``tests/test_turn_identity.py``). No user text
    answers yes: the gate only catches positive evidence of someone else's turn.
    """
    if not prompt or not prompt.strip():
        # No prompt to claim; answer yes so the job soaks up the next turn.
        return True
    if not heard:
        return True
    needle = " ".join(prompt.split())[:PROMPT_MATCH]
    return any(needle in " ".join(text.split()) for text in heard)


@dataclass(frozen=True, slots=True)
class Turn:
    """One finished turn: what the agent said, and what it was replying to.

    A record rather than a pair, because both halves are sequences of text and
    a caller that transposes them gets no complaint from the type checker.
    """

    #: Assistant text, blank-line joined in arrival order.
    said: str
    #: User text that arrived during the turn, in arrival order.
    heard: tuple[str, ...] = ()
    #: Assistant text before parser clipping, blank-line joined in arrival order.
    raw_said: str = ""


@dataclass
class TurnAccumulator:
    """What one participant has said since its last turn boundary.

    Lives as long as the watcher: a per-``_apply`` local lost text split across polls and
    kept only the last fragment. Kept apart from ``QuietClock`` on purpose (time vs conversation).
    """

    #: Assistant text seen since the last boundary, in arrival order.
    _blocks: list[str] = field(default_factory=list)
    #: Assistant text before clipping, in arrival order.
    _raw_blocks: list[str] = field(default_factory=list)
    #: User text seen since the last boundary.
    _heard: list[str] = field(default_factory=list)
    #: Turn ids already handled, newest last; set answers, deque decides what to forget.
    _answered: deque[str] = field(default_factory=deque)
    _seen: set[str] = field(default_factory=set)

    def say(self, text: str, raw_text: str | None = None) -> None:
        if text or raw_text:
            self._blocks.append(text)
            self._raw_blocks.append(raw_text if raw_text is not None else text)

    def hear(self, text: str) -> None:
        if text:
            self._heard.append(text)

    def take(self) -> Turn:
        """The finished turn, and forget it. Text blank-line joined, as written."""
        turn = Turn(
            said="\n\n".join(self._blocks),
            heard=tuple(self._heard),
            raw_said="\n\n".join(self._raw_blocks),
        )
        self._blocks.clear()
        self._raw_blocks.clear()
        self._heard.clear()
        return turn

    def already_handled(self, turn_id: str | None) -> bool:
        """Has this exact turn already been dealt with (answered or deliberately declined)?

        Unmarked declines would let Claude's duplicate boundary answer after all. An
        unidentified boundary is never a duplicate.
        """
        return turn_id is not None and turn_id in self._seen

    def mark_handled(self, turn_id: str | None) -> None:
        if turn_id is None or turn_id in self._seen:
            return
        self._answered.append(turn_id)
        self._seen.add(turn_id)
        while len(self._answered) > ANSWERED_TURNS:
            self._seen.discard(self._answered.popleft())
