"""Parser behaviour shared by every harness: unparseable lines, text clipping and idle prompts."""

from __future__ import annotations

import json

import pytest

from tests.support.parse_rig import events_for
from tests.support.paths import FIXTURES
from tests.support.shipped import ClaudeCodeObserver, CodexObserver, OpenCodeObserver, VibeObserver
from theater.harness import EventKind
from theater.harness.base import MAX_TEXT, Event
from theater.harness.observation import HarnessObserver, ScreenConfidence, ScreenKind, ScreenReading


def test_event_positional_constructor_keeps_tool_name_position():
    event = Event(EventKind.TOOL_CALL, "calling", "Read")
    assert event.text == "calling"
    assert event.tool_name == "Read"
    assert event.raw_text is None
    assert event.answer_final is False


def test_answer_final_requires_a_boundary():
    with pytest.raises(ValueError, match="answer_final requires turn_end"):
        Event(EventKind.ASSISTANT, answer_final=True)


@pytest.mark.parametrize(
    ("observer", "fixture", "first_ts"),
    [
        pytest.param(ClaudeCodeObserver(), "claude_code.jsonl", 1782325575.348, id="claude"),
        pytest.param(CodexObserver(), "codex.jsonl", 1786534852.2, id="codex"),
    ],
)
def test_records_carry_their_own_timestamp(observer, fixture, first_ts):
    events = events_for(observer, FIXTURES / fixture)
    assert all(e.ts is not None for e in events)
    # ISO-8601 with a Z suffix, parsed as UTC rather than local time.
    assert events[0].ts == pytest.approx(first_ts, abs=0.01)


@pytest.mark.parametrize(
    ("observer", "fixture", "raw_indexes"),
    [
        pytest.param(ClaudeCodeObserver(), "claude_code.jsonl", [0, 1, 2, 3, 4, 5, 6], id="claude"),
        pytest.param(CodexObserver(), "codex.jsonl", [2, 3, 4, 5, 7, 10], id="codex"),
    ],
)
def test_raw_index_tracks_the_source_record(observer, fixture, raw_indexes):
    events = events_for(observer, FIXTURES / fixture)
    assert [e.raw_index for e in events] == raw_indexes


@pytest.mark.parametrize("observer", [ClaudeCodeObserver(), CodexObserver()])
@pytest.mark.parametrize("line", ["not json", "null"])
def test_unparseable_lines_yield_nothing_rather_than_raising(observer, line):
    """A transcript being appended to as we read it is normal, not an error."""
    assert observer.parse(line, 0) == []


def test_long_text_is_clipped_before_it_reaches_the_bus():
    body = "x" * (MAX_TEXT * 3)
    events = VibeObserver().parse(json.dumps({"role": "user", "content": body}), 0)
    text = events[0].text
    assert len(text) < len(body)
    assert text.startswith("x" * 100)
    assert text.endswith(f"(+{MAX_TEXT * 2} chars)")


LONG = "x" * (MAX_TEXT * 3)


#: One record per branch that carries text, so the parametrization fails if a
#: branch forgets to honour clip_text. That has happened: a Claude Code user
#: text block clipped unconditionally, which `read_transcript` then served as
#: the "full" text.
UNCLIPPED_CASES = [
    (VibeObserver(), {"role": "user", "content": LONG}),
    (
        ClaudeCodeObserver(),
        {"type": "assistant", "message": {"content": [{"type": "text", "text": LONG}]}},
    ),
    (
        CodexObserver(),
        {"type": "event_msg", "payload": {"type": "user_message", "message": LONG}},
    ),
]


@pytest.mark.parametrize("observer,record", UNCLIPPED_CASES)
def test_clip_text_false_returns_the_whole_text(observer, record):
    """read_transcript asks for the record as written, on every branch."""
    events = observer.parse(json.dumps(record), 0, clip_text=False)
    assert events, "expected the record to parse into at least one event"
    assert any(e.text == LONG for e in events)


@pytest.mark.parametrize("observer,record", UNCLIPPED_CASES)
def test_the_same_branches_clip_by_default(observer, record):
    events = observer.parse(json.dumps(record), 0)
    assert events
    assert all(len(e.text) <= MAX_TEXT + 40 for e in events)


RAW_TEXT_CASES = [
    (
        ClaudeCodeObserver(),
        {"type": "assistant", "message": {"content": [{"type": "text", "text": LONG}]}},
    ),
    (
        CodexObserver(),
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": LONG},
        },
    ),
    (VibeObserver(), {"role": "assistant", "content": LONG}),
]


@pytest.mark.parametrize("observer,record", RAW_TEXT_CASES)
def test_builtin_parsers_preserve_raw_text_before_clipping(observer, record):
    events = observer.parse(json.dumps(record), 0)
    event = next(e for e in events if e.raw_text == LONG)
    assert event.text != LONG
    assert event.text.endswith(f"(+{MAX_TEXT * 2} chars)")


@pytest.mark.parametrize(
    "observer", [ClaudeCodeObserver(), CodexObserver(), OpenCodeObserver(), VibeObserver()]
)
@pytest.mark.parametrize("capture", [""])
def test_a_blank_pane_is_not_a_prompt(observer, capture):
    """An empty capture means the pane has not drawn, not that it is waiting."""
    assert observer.is_idle_screen(capture) is False


@pytest.mark.parametrize(
    "observer,capture",
    [
        (VibeObserver(), "some output\n❯"),
        (VibeObserver(), "some output\n❯ "),
        (VibeObserver(), "some output\n❯\n\n"),
        (ClaudeCodeObserver(), "some output\n>"),
        (ClaudeCodeObserver(), "some output\n> "),
    ],
)
def test_a_bare_prompt_on_the_last_line_is_idle(observer, capture):
    assert observer.is_idle_screen(capture) is True


@pytest.mark.parametrize(
    "observer,capture",
    [
        (VibeObserver(), "❯ what model are you"),
        (VibeObserver(), "❯\nstill rendering output"),
        (ClaudeCodeObserver(), "> what model are you"),
        (ClaudeCodeObserver(), "> \nstill rendering output"),
        (OpenCodeObserver(), "loading\n"),
    ],
)
def test_text_after_the_prompt_is_not_idle(observer, capture):
    """Someone typing is presence, and output still landing is work."""
    assert observer.is_idle_screen(capture) is False


class _BooleanOnlyObserver(HarnessObserver):
    """A stub observer that implements only ``is_idle_screen``.

    This is the shape a third-party plugin in ``$THEATER_HOME/plugins`` has
    today: it answers the boolean and nothing else. The default
    ``screen_reading`` shim must keep it working without any code change.
    """

    def __init__(self, idle: bool):
        self._idle = idle

    def is_idle_screen(self, capture: str) -> bool:
        return self._idle


def test_a_boolean_idle_observer_yields_prompt_with_low_confidence():
    """The default shim maps True to kind=prompt, confidence=low."""
    observer = _BooleanOnlyObserver(idle=True)
    reading = observer.screen_reading("anything")
    assert reading == ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.LOW)


def test_a_boolean_not_idle_observer_yields_unknown_with_low_confidence():
    """The default shim maps False to kind=unknown, confidence=low.

    ``unknown`` rather than ``working`` so that a future send gate — which must
    never falsely conclude "blocked" — treats a low-confidence non-idle screen
    as "do not know" rather than "safe to send". The consumer resolves
    ``unknown``, not the type.
    """
    observer = _BooleanOnlyObserver(idle=False)
    reading = observer.screen_reading("anything")
    assert reading == ScreenReading(kind=ScreenKind.UNKNOWN, confidence=ScreenConfidence.LOW)
