"""OpenCode history reads current projection rows without clipping or counting usage as talk."""

from __future__ import annotations

import asyncio

import pytest

from tests.support.opencode_recorder_v2 import USAGE, text, tool
from tests.support.opencode_recorder_v2 import rec as shared_rec
from theater.harness import EventKind, TurnTerminal
from theater.harness.base import MAX_TEXT
from theater.harness.builtin.plugins.opencode.constants import HISTORY_MESSAGE_BATCH
from theater.harness.builtin.plugins.opencode.source_v2 import OpenCodeV2Source

rec = shared_rec


def test_history_reads_a_tool_turn_without_polling_and_bounds_visible_events(rec, workdir):
    rec.user("msg_u1", "read the note")
    rec.step(
        "msg_a1",
        rec.tick(),
        [tool("call_one", "completed", str(workdir / "note"), "contents")],
        finish="tool-calls",
        times={"completed": rec.tick()},
        **USAGE,
    )
    rec.step("msg_a2", rec.tick(), [text("answer")], finish="stop", times={"completed": rec.tick()})
    rec.idle("msg_i1", "succeeded")
    src = OpenCodeV2Source(rec.path, cwd=str(workdir), session_id=rec.sid)

    history = asyncio.run(src.history(last_n=0))
    assert history.location == "opencode://ses_one"
    assert [event.kind for event in history.events if not event.usage_only] == [
        EventKind.USER,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.ASSISTANT,
    ]
    recent = asyncio.run(src.history(last_n=2))
    assert [event.kind for event in recent.events if not event.usage_only] == [
        EventKind.TOOL_RESULT,
        EventKind.ASSISTANT,
    ]


def test_history_does_not_clip_large_completed_text(rec, workdir):
    long = "x" * (MAX_TEXT + 500)
    rec.step("msg_a1", rec.tick(), [text(long)], finish="stop", times={"completed": rec.tick()})
    src = OpenCodeV2Source(rec.path, cwd=str(workdir))
    assert asyncio.run(src.history(last_n=0)).events[-1].text == long


@pytest.mark.parametrize(
    "outcome,terminal",
    [
        ("succeeded", TurnTerminal.COMPLETED),
        ("failed", TurnTerminal.FAILED),
        ("interrupted", TurnTerminal.INTERRUPTED),
    ],
)
def test_cold_history_classifies_the_idle_boundary(rec, workdir, outcome, terminal):
    rec.user("msg_u1", "do the thing")
    rec.step(
        "msg_a1",
        rec.tick(),
        [text("partial")],
        finish="tool-calls",
        times={"completed": rec.tick()},
    )
    rec.idle("msg_i1", outcome)
    history = asyncio.run(OpenCodeV2Source(rec.path, cwd=str(workdir)).history(last_n=0))
    ends = [event for event in history.events if event.turn_end]
    assert len(ends) == 1 and ends[0].turn_terminal is terminal


def test_bounded_history_scans_past_usage_only_batches(rec, workdir):
    rec.user("old-user", "old")
    for index in range(HISTORY_MESSAGE_BATCH + 1):
        rec.step(
            f"usage-{index}",
            rec.tick(),
            [],
            finish="tool-calls",
            times={"completed": rec.tick()},
            **USAGE,
        )
    rec.user("new-user", "new")
    history = asyncio.run(OpenCodeV2Source(rec.path, cwd=str(workdir)).history(last_n=2))
    assert [event.text for event in history.events if not event.usage_only] == ["old", "new"]


def test_history_of_a_directory_without_a_session_is_empty(rec, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    history = asyncio.run(OpenCodeV2Source(rec.path, cwd=str(other)).history(last_n=0))
    assert history.location is None and not history.events
