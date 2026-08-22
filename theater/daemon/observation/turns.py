"""Turn boundary accumulation and prompt matching.

Pure value objects with no dependency on the registry, store, or harness
plugins. These are the conversation-state half of observation: what the agent
said, and what it was replying to, accumulated across poll boundaries.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field

from theater.constants.observation import ANSWERED_TURNS, PROMPT_MATCH


def answers_prompt(heard: Sequence[str], prompt: str | None) -> bool:
    """Did this turn begin with the prompt we injected?

    Every harness Theater drives echoes an injected prompt back as a user
    record before the reply — verified against captures of one real
    round-trip per harness in ``tests/test_turn_identity.py``, not assumed. So
    the user text a turn opens with says who the turn belongs to, and a turn
    that opens with something else belongs to whoever typed it.

    Absence of evidence answers yes. A participant we attached to mid-turn, a
    harness that keeps no user record, and the screen-derived boundary of a
    harness with no transcript at all have no user text to offer, and refusing
    to answer there would hang every caller of those. The gate exists to catch
    positive evidence that a turn is *someone else's*, and nothing weaker.

    Matching is a normalised prefix rather than equality, in both directions:
    whitespace survives injection unreliably, the reported text is clipped,
    and a harness is free to wrap the prompt in scaffolding of its own.
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

    Lives for as long as the watcher does, which is the whole point. The text
    used to be a local rebuilt on every ``_apply`` call, so a turn whose text
    arrived in one poll and whose boundary arrived in the next answered the
    waiting job with an empty string. It also only ever held the *last*
    assistant fragment, so a Claude reply written as three text blocks came
    back as its final paragraph alone.

    Kept apart from ``QuietClock`` deliberately: that class is the observer's
    sense of time passing and says so in its own docstring. This is
    conversation state. They have the same lifetime and nothing else in common.
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
        """Has this exact turn already been dealt with?

        Dealt with, not answered: a turn we deliberately declined to answer
        because it was a human's is handled too. Were it not marked, Claude's
        duplicate boundary would arrive with the accumulator already emptied,
        find no user text, read that as no evidence, and answer after all.

        An unidentified boundary is never a duplicate: a harness that publishes
        no turn id gets one answer per boundary, which is what it had before.
        """
        return turn_id is not None and turn_id in self._seen

    def mark_handled(self, turn_id: str | None) -> None:
        if turn_id is None or turn_id in self._seen:
            return
        self._answered.append(turn_id)
        self._seen.add(turn_id)
        while len(self._answered) > ANSWERED_TURNS:
            self._seen.discard(self._answered.popleft())
