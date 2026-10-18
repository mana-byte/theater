"""OpenCode turns read from storage events, and the idle footer."""

from __future__ import annotations

import asyncio

from tests.support.opencode_recorder import rec as shared_rec
from tests.support.opencode_storage import a_turn_with_a_tool, drain
from tests.support.shipped import OpenCodeObserver
from theater.harness import EventKind

rec = shared_rec  # the shared fixture, under the name the tests ask for


def test_a_turn_reads_as_one_user_one_tool_pair_and_one_reply(rec, workdir):
    src = drain(rec, workdir)
    a_turn_with_a_tool(rec)

    batch = asyncio.run(src.read())

    assert [e.kind for e in batch.events] == [
        EventKind.USER,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.ASSISTANT,
    ]
    assert batch.progressed is True
    assert batch.events[0].text == "read the note"
    assert batch.events[1].tool_name == "read"
    assert batch.events[2].text == "the secret is pamplemousse"
    assert batch.events[3].text == "pamplemousse"


def test_event_backlog_requests_immediate_repoll(rec, workdir, monkeypatch):
    from theater.harness.builtin.plugins.opencode import parser as parser_module

    monkeypatch.setattr(parser_module, "DRAIN_LIMIT", 2)
    src = drain(rec, workdir)
    for _ in range(3):
        rec.emit("session.updated.1", {})

    first = asyncio.run(src.read())
    second = asyncio.run(src.read())

    assert first.has_more is True
    assert second.has_more is False


def test_only_the_last_step_ends_the_turn(rec, workdir):
    """`finish == "tool-calls"` is a step. Ending the turn there would hand a
    waiting caller the empty text of a message that only called a tool."""
    src = drain(rec, workdir)
    a_turn_with_a_tool(rec)

    ends = [e.turn_end for e in asyncio.run(src.read()).events]

    assert ends == [False, False, False, True]


def test_a_step_that_only_called_tools_produces_no_assistant_line(rec, workdir):
    src = drain(rec, workdir)
    a_turn_with_a_tool(rec)

    said = [e for e in asyncio.run(src.read()).events if e.kind is EventKind.ASSISTANT]

    assert len(said) == 1


def test_the_finish_that_fires_twice_is_reported_once(rec, workdir):
    src = drain(rec, workdir)
    info = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_t1", "hello")
    rec.finish(info, "stop")

    events = asyncio.run(src.read()).events

    assert [e.kind for e in events] == [EventKind.ASSISTANT]
    assert events[0].text == "hello"


def test_finish_usage_is_reported_once_with_full_model_identity(rec, workdir):
    src = drain(rec, workdir)
    info = rec.message("msg_a1", "assistant")
    info.update(
        providerID="openai-foundry",
        modelID="zai-glm-5-2",
        cost=0,
        tokens={
            "input": 11,
            "output": 7,
            "reasoning": 2,
            "cache": {"read": 5, "write": 3},
        },
    )
    rec.text("msg_a1", "prt_t1", "hello")
    rec.finish(info, "stop")

    events = asyncio.run(src.read()).events

    assert len(events) == 1
    usage = events[0].usage
    assert usage is not None
    assert usage.model == "openai-foundry/zai-glm-5-2"
    assert usage.cost_usd is None
    assert usage.input_tokens == 11
    assert usage.output_tokens == 7
    assert usage.cache_read_input_tokens == 5
    assert usage.cache_creation_input_tokens == 3
    assert usage.reasoning_output_tokens == 2
    assert usage.idempotency_key == "opencode:msg_a1"


def test_usage_falls_back_to_model_id_without_provider(rec, workdir):
    src = drain(rec, workdir)
    info = rec.message("msg_a1", "assistant")
    info.update(modelID="gpt-5", tokens={"input": 1, "output": 2})
    rec.text("msg_a1", "prt_t1", "hello")
    rec.finish(info, "stop")

    usage = asyncio.run(src.read()).events[0].usage

    assert usage is not None
    assert usage.model == "gpt-5"


def test_streamed_text_is_emitted_once_whole_at_the_end(rec, workdir):
    """Not once per update: the bus would carry three prefixes of one reply,
    and the job would resolve with whichever fragment landed last."""
    src = drain(rec, workdir)
    info = rec.message("msg_a1", "assistant")
    rec._part({"id": "p", "messageID": "msg_a1", "type": "text", "text": "par"})
    rec._part({"id": "p", "messageID": "msg_a1", "type": "text", "text": "parti"})

    assert not asyncio.run(src.read()).events

    rec._part({"id": "p", "messageID": "msg_a1", "type": "text", "text": "partial"})
    rec.finish(info, "stop")
    events = asyncio.run(src.read()).events

    assert [(e.kind, e.text) for e in events] == [(EventKind.ASSISTANT, "partial")]


def test_a_reply_written_in_several_parts_is_joined_in_order(rec, workdir):
    src = drain(rec, workdir)
    info = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_1", "first ")
    rec.text("msg_a1", "prt_2", "second")
    rec.finish(info, "stop")

    assert asyncio.run(src.read()).events[0].text == "first second"


def test_a_tool_call_is_reported_once_however_many_updates_it_takes(rec, workdir):
    src = drain(rec, workdir)
    rec.message("msg_a1", "assistant")
    rec.tool("msg_a1", "prt_tool", "call_1", "read", "contents")

    events = asyncio.run(src.read()).events

    assert [e.kind for e in events] == [EventKind.TOOL_CALL, EventKind.TOOL_RESULT]
    assert all(e.tool_name == "read" for e in events)


def test_a_pending_tool_call_is_not_reported(rec, workdir):
    """Pending carries no arguments yet and may never run."""
    src = drain(rec, workdir)
    rec.message("msg_a1", "assistant")
    rec._part(
        {
            "id": "prt_tool",
            "messageID": "msg_a1",
            "type": "tool",
            "callID": "call_1",
            "tool": "read",
            "state": {"status": "pending", "input": {}},
        }
    )

    assert not asyncio.run(src.read()).events


def test_a_failed_tool_call_still_produces_a_result(rec, workdir):
    src = drain(rec, workdir)
    rec.message("msg_a1", "assistant")
    rec._part(
        {
            "id": "prt_tool",
            "messageID": "msg_a1",
            "type": "tool",
            "callID": "call_1",
            "tool": "bash",
            "state": {"status": "error", "error": "exit 1"},
        }
    )

    events = asyncio.run(src.read()).events

    assert [e.kind for e in events] == [EventKind.TOOL_CALL, EventKind.TOOL_RESULT]
    assert events[1].text == "exit 1"


def test_a_user_part_belonging_to_a_message_we_skipped_is_still_a_user_part(rec, workdir):
    """The role comes from the `message` table when the stream did not say."""
    user = rec.message("msg_u1", "user")
    src = drain(rec, workdir)
    rec.user_text(user["id"], "prt_u1", "late")

    events = asyncio.run(src.read()).events

    assert [(e.kind, e.text) for e in events] == [(EventKind.USER, "late")]


def test_bookkeeping_events_are_progress_without_being_conversation(rec, workdir):
    """Silence here would let the rescue timer fire mid-turn."""
    src = drain(rec, workdir)
    rec.emit("session.updated.1", {"info": {"id": "ses_one"}})

    batch = asyncio.run(src.read())

    assert not batch.events
    assert batch.progressed is True


def test_a_reply_is_stamped_with_when_it_ended(rec, workdir):
    """The first finish has no `time.completed` and the second is deduped, so
    the stamp has to come from the last part the message produced."""
    src = drain(rec, workdir)
    info = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_1", "done")
    rec.step_finish("msg_a1", "prt_sf", "stop")
    started = info["time"]["created"] / 1000
    rec.finish(info, "stop")

    assert asyncio.run(src.read()).events[0].ts > started


#: The footer opencode draws once a conversation exists. The `Ask anything...`
#: placeholder is gone by then, which is why there is no positive marker.
IDLE = "\n".join(["> ", "~/work  90.7K       ctrl+p commands"])


WORKING = "\n".join(["> ", "esc interrupt       ctrl+p commands"])


def test_idle_when_the_footer_is_not_offering_to_interrupt():
    assert OpenCodeObserver().is_idle_screen(IDLE) is True


def test_not_idle_while_a_turn_is_running():
    assert OpenCodeObserver().is_idle_screen(WORKING) is False
