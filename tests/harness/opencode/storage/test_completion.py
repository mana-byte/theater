"""Supported projection preserves tool continuation and terminal boundaries."""

from __future__ import annotations

import asyncio

import pytest

from tests.support.opencode_recorder import attached, drain, text, tool
from tests.support.opencode_recorder import rec as shared_rec
from theater.harness import EventKind
from theater.harness.builtin.plugins.opencode.source import OpenCodeSource
from theater.models import Status

rec = shared_rec


@pytest.mark.parametrize(
    "state,executed,continues",
    [
        ({"status": "completed", "content": [{"type": "text", "text": "ok"}]}, False, True),
        ({"status": "error", "error": {"type": "command", "message": "exit 1"}}, False, True),
        ({"status": "completed", "content": []}, True, False),
        ({"status": "error", "error": {"type": "aborted", "message": "interrupted"}}, False, False),
    ],
    ids=["local-tool", "local-error", "provider-executed", "interrupted-orphan"],
)
def test_stop_with_tools_uses_native_continuation_rules(rec, workdir, state, executed, continues):
    src = attached(rec, workdir)
    part = tool("call_one", "completed", str(workdir / "note"))
    part.update(state=state, executed=executed)
    rec.step(
        "msg_a1", rec.tick(), [text("answer"), part], finish="stop", times={"completed": rec.tick()}
    )
    events = drain(src)

    assert [event.kind for event in events] == [
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.ASSISTANT,
    ]
    assert events[-1].turn_end is not continues
    cold = OpenCodeSource(rec.path, cwd=str(workdir))
    assert asyncio.run(cold.read()).status is (Status.WORKING if continues else Status.IDLE)
    stored = asyncio.run(src.history(last_n=0)).events
    assert [(e.kind, e.text, e.turn_end) for e in stored] == [
        (e.kind, e.text, e.turn_end) for e in events
    ]


@pytest.mark.parametrize("finish", [None, "unknown", "tool-calls"])
def test_incomplete_steps_wait_for_one_final_boundary(rec, workdir, finish):
    src = attached(rec, workdir)
    rec.step(
        "msg_a1", rec.tick(), [text("partial")], finish=finish, times={"completed": rec.tick()}
    )
    first = drain(src)
    assert not any(event.turn_end for event in first)
    rec.step("msg_a2", rec.tick(), [text("done")], finish="stop", times={"completed": rec.tick()})
    rec.idle("msg_i1", "succeeded")
    events = first + drain(src)
    assert [e.text for e in events if e.turn_end] == ["done"]
    assert drain(src) == []


def test_has_tool_calls_excludes_only_provider_and_orphan_parts():
    """The helper mirrors native hasToolCalls exactly: every tool part
    counts except provider-executed ones and cleanup-marked interrupted
    orphans; odd rows are ignored."""
    from theater.harness.builtin.plugins.opencode.values import _has_tool_calls

    tool = {"type": "tool"}
    assert _has_tool_calls([tool])
    assert not _has_tool_calls([])
    assert not _has_tool_calls([{"type": "text", "text": "words"}])
    assert not _has_tool_calls([{**tool, "metadata": {"providerExecuted": True}}])
    assert not _has_tool_calls(
        [{**tool, "state": {"status": "error", "metadata": {"interrupted": True}}}]
    )
    assert _has_tool_calls([{**tool, "state": {"status": "error"}}])
    assert _has_tool_calls([{"type": "text"}, tool, None, "junk"])
    assert not _has_tool_calls([None, 3, "junk"])


def test_an_unknown_finish_is_a_partial_trajectory_step():
    """The trajectory projection must not present an `unknown` step as a
    completed assistant record while the native loop is still running."""
    from theater.harness.builtin.plugins.opencode.values import _finish_status
    from theater.trajectory.enums import TrajectoryStatus

    assert _finish_status("tool-calls") is TrajectoryStatus.PARTIAL
    assert _finish_status("unknown") is TrajectoryStatus.PARTIAL
    assert _finish_status("stop") is TrajectoryStatus.COMPLETED
    assert _finish_status("stop", has_tool_calls=True) is TrajectoryStatus.PARTIAL
    assert _finish_status(None) is TrajectoryStatus.RUNNING
