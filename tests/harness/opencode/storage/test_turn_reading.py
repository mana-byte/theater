"""Current projection preserves text, tool state, usage and screen hints."""

from __future__ import annotations

import asyncio

from tests.support.opencode_recorder_v2 import attached, drain, text, tool
from tests.support.opencode_recorder_v2 import rec as shared_rec
from tests.support.shipped import OpenCodeObserver
from theater.harness import EventKind

rec = shared_rec


def test_finish_usage_is_reported_once_with_full_model_identity(rec, workdir):
    src = attached(rec, workdir)
    created = rec.tick()
    fields = {
        "finish": "stop",
        "times": {"completed": rec.tick()},
        "model": {"id": "zai-glm-5-2", "providerID": "openai-foundry"},
        "cost": 0,
        "tokens": {"input": 11, "output": 7, "reasoning": 2, "cache": {"read": 5, "write": 3}},
    }
    rec.step("msg_a1", created, [text("hello")], **fields)
    events = drain(src)
    assert len(events) == 1
    usage = events[0].usage
    assert usage is not None and usage.model == "openai-foundry/zai-glm-5-2"
    assert usage.cost_usd is None
    assert (usage.input_tokens, usage.output_tokens, usage.reasoning_output_tokens) == (11, 7, 2)
    assert (usage.cache_read_input_tokens, usage.cache_creation_input_tokens) == (5, 3)
    assert usage.idempotency_key == "opencode:msg_a1"
    rec.step("msg_a1", created, [text("hello")], **fields)
    assert drain(src) == []


def test_usage_falls_back_to_model_id_without_provider(rec, workdir):
    src = attached(rec, workdir)
    rec.step(
        "msg_a1",
        rec.tick(),
        [text("hello")],
        finish="stop",
        times={"completed": rec.tick()},
        model={"id": "gpt-5"},
        tokens={"input": 1, "output": 2},
    )
    usage = drain(src)[0].usage
    assert usage is not None and usage.model == "gpt-5"


def test_streamed_text_is_emitted_once_whole_at_the_end(rec, workdir):
    src = attached(rec, workdir)
    created = rec.tick()
    for prefix in ("par", "parti"):
        rec.step("msg_a1", created, [text(prefix)])
        assert drain(src) == []
    rec.step("msg_a1", created, [text("partial")], finish="stop", times={"completed": rec.tick()})
    assert [(event.kind, event.text) for event in drain(src)] == [(EventKind.ASSISTANT, "partial")]


def test_a_reply_written_in_several_parts_is_joined_in_order(rec, workdir):
    src = attached(rec, workdir)
    rec.step(
        "msg_a1",
        rec.tick(),
        [text("first "), text("second")],
        finish="stop",
        times={"completed": rec.tick()},
    )
    assert drain(src)[0].text == "first second"


def test_pending_tool_is_silent_then_reports_one_call_and_result(rec, workdir):
    src = attached(rec, workdir)
    created = rec.tick()
    rec.step("msg_a1", created, [tool("call_one", "streaming", "note")])
    assert drain(src) == []
    rec.step("msg_a1", created, [tool("call_one", "running", "note")])
    assert [event.kind for event in drain(src)] == [EventKind.TOOL_CALL]
    for _ in range(2):
        rec.step("msg_a1", created, [tool("call_one", "completed", "note", "contents")])
    assert [(e.kind, e.text) for e in drain(src)] == [(EventKind.TOOL_RESULT, "contents")]
    assert drain(src) == []


def test_failed_tool_call_preserves_its_error_result(rec, workdir):
    src = attached(rec, workdir)
    part = tool("call_one", "error", "note")
    part["state"]["error"] = {"type": "command", "message": "exit 1"}
    rec.step("msg_a1", rec.tick(), [part])
    events = drain(src)
    assert [event.kind for event in events] == [EventKind.TOOL_CALL, EventKind.TOOL_RESULT]
    assert events[1].text == "exit 1"


def test_an_attached_empty_user_row_can_later_acquire_its_text(rec, workdir):
    rec.user("msg_u1", "")
    src = attached(rec, workdir)
    rec.user("msg_u1", "late")
    assert [(event.kind, event.text) for event in drain(src)] == [(EventKind.USER, "late")]


def test_a_changed_unfinished_step_is_progress_without_conversation(rec, workdir):
    src = attached(rec, workdir)
    rec.step("msg_a1", rec.tick(), [])
    batch = asyncio.run(src.read())
    assert batch.events == [] and batch.progressed


def test_a_reply_is_stamped_with_when_it_ended(rec, workdir):
    src = attached(rec, workdir)
    created, completed = rec.tick(), rec.tick()
    rec.step("msg_a1", created, [text("done")], finish="stop", times={"completed": completed})
    assert drain(src)[0].ts == completed / 1000


#: The footer opencode draws once a conversation exists. The `Ask anything...`
#: placeholder is gone by then, which is why there is no positive marker.
IDLE = "\n".join(["> ", "~/work  90.7K       ctrl+p commands"])


WORKING = "\n".join(["> ", "esc interrupt       ctrl+p commands"])


def test_idle_when_the_footer_is_not_offering_to_interrupt():
    assert OpenCodeObserver().is_idle_screen(IDLE) is True


def test_not_idle_while_a_turn_is_running():
    assert OpenCodeObserver().is_idle_screen(WORKING) is False
