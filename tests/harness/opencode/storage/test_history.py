"""OpenCode bounded history rebuilt from stored state."""

from __future__ import annotations

import asyncio

from tests.support.opencode_recorder import rec as shared_rec
from tests.support.opencode_storage import a_turn_with_a_tool, drain, source_for
from theater.harness import EventKind, TurnTerminal
from theater.harness.builtin.plugins.opencode.constants import HISTORY_MESSAGE_BATCH

rec = shared_rec  # the shared fixture


def test_history_rebuilds_the_conversation_from_stored_state(rec, workdir):
    a_turn_with_a_tool(rec)
    src = source_for(rec, workdir)

    history = asyncio.run(src.history(last_n=0))

    assert history.location == "opencode://ses_one"
    assert [e.kind for e in history.events] == [
        EventKind.USER,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.ASSISTANT,
    ]
    assert history.events[-1].text == "pamplemousse"
    assert history.events[-1].turn_end is True


def test_history_reads_without_having_polled(rec, workdir):
    """`read_transcript` opens its own source and never calls `read()`."""
    a_turn_with_a_tool(rec)
    src = source_for(rec, workdir, session_id="ses_one")
    assert asyncio.run(src.history(last_n=0)).events


def test_history_does_not_clip(rec, workdir):
    from theater.harness.base import MAX_TEXT

    long = "x" * (MAX_TEXT + 500)
    info = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_1", long)
    rec.finish(info, "stop")

    history = asyncio.run(source_for(rec, workdir).history(last_n=0))

    assert history.events[-1].text == long


def test_history_returns_the_newest_events_when_asked_for_a_few(rec, workdir):
    a_turn_with_a_tool(rec)
    history = asyncio.run(source_for(rec, workdir).history(last_n=2))
    assert [e.kind for e in history.events] == [
        EventKind.TOOL_RESULT,
        EventKind.ASSISTANT,
    ]


def test_the_live_finish_answers_completed(rec, workdir):
    """A live turn's finish carries completed, not an unknown outcome."""
    src = drain(rec, workdir)
    info = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_t1", "hello")
    rec.finish(info, "stop")

    boundaries = [e for e in asyncio.run(src.read()).events if e.turn_end]

    assert boundaries and boundaries[-1].turn_terminal is TurnTerminal.COMPLETED


def test_history_marks_a_successful_turn_completed(rec, workdir):
    """The stored row a live finish wrote replays with the same outcome."""
    a_turn_with_a_tool(rec)

    history = asyncio.run(source_for(rec, workdir).history(last_n=0))

    boundaries = [e for e in history.events if e.turn_end]
    assert boundaries and boundaries[-1].turn_terminal is TurnTerminal.COMPLETED


def test_history_marks_a_failed_turn_failed(rec, workdir):
    info = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_1", "partial")
    rec._store_message("msg_a1", dict(info, error={"name": "ApiError", "message": "boom"}))

    history = asyncio.run(source_for(rec, workdir).history(last_n=0))

    boundaries = [e for e in history.events if e.turn_end]
    assert boundaries and boundaries[-1].turn_terminal is TurnTerminal.FAILED


def test_history_marks_an_aborted_turn_interrupted(rec, workdir):
    info = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_1", "partial")
    rec._store_message(
        "msg_a1", dict(info, error={"name": "AbortedError", "message": "User aborted"})
    )

    history = asyncio.run(source_for(rec, workdir).history(last_n=0))

    boundaries = [e for e in history.events if e.turn_end]
    assert boundaries and boundaries[-1].turn_terminal is TurnTerminal.INTERRUPTED


def test_bounded_history_scans_older_batches_until_it_has_enough_events(rec, workdir):
    rec.message("old-user", "user")
    rec.user_text("old-user", "old-part", "old")
    for index in range(HISTORY_MESSAGE_BATCH):
        rec.message(f"ignored-{index}", "unknown")
    rec.message("new-user", "user")
    rec.user_text("new-user", "new-part", "new")

    history = asyncio.run(source_for(rec, workdir).history(last_n=2))

    assert [event.text for event in history.events] == ["old", "new"]


def test_bounded_history_does_not_count_usage_only_events(rec, workdir):
    rec.message("old-user", "user")
    rec.user_text("old-user", "old-part", "old")
    usage = rec.message("usage-only", "assistant")
    usage.update(modelID="gpt-5", tokens={"input": 1, "output": 0})
    rec._store_message("usage-only", usage)
    rec.message("new-user", "user")
    rec.user_text("new-user", "new-part", "new")

    history = asyncio.run(source_for(rec, workdir).history(last_n=2))

    assert [event.text for event in history.events] == ["old", "new"]


def test_history_of_a_session_that_is_not_there_is_empty(rec, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    history = asyncio.run(source_for(rec, other).history(last_n=0))
    assert history.location is None
    assert not history.events


def test_the_live_path_and_history_agree(rec, workdir):
    """Two readers of the same session must not tell different stories."""
    src = drain(rec, workdir)
    a_turn_with_a_tool(rec)

    live = asyncio.run(src.read()).events
    stored = asyncio.run(src.history(last_n=0)).events

    assert [(e.kind, e.text, e.turn_end) for e in live] == [
        (e.kind, e.text, e.turn_end) for e in stored
    ]
