"""OpenCode 2.x observer: turn reading, same-timestamp drains and compaction usage."""

from __future__ import annotations

import asyncio

import pytest

from tests.support.opencode_recorder_v2 import (
    USAGE,
    attached,
    drain,
    text,
    tool,
)
from tests.support.opencode_recorder_v2 import rec as shared_rec
from theater.harness import EventKind, TurnTerminal
from theater.harness.base import EventPath
from theater.harness.builtin.plugins.opencode import source_v2
from theater.harness.builtin.plugins.opencode.source_v2 import OpenCodeV2Source
from theater.models import Status

rec = shared_rec  # the shared fixture, under the name the tests ask for


def a_turn_with_a_tool(rec, workdir) -> None:
    note = str(workdir / "note.txt")
    rec.user("msg_u1", "read the note")
    first = rec.tick()
    rec.step("msg_a1", first, [tool("call_1", "running", note)])
    rec.step(
        "msg_a1",
        first,
        [tool("call_1", "completed", note, "the secret is pamplemousse")],
        finish="tool-calls",
        times={"completed": rec.tick()},
        **USAGE,
    )
    second = rec.tick()
    rec.step("msg_a2", second, [text("")])
    rec.step(
        "msg_a2",
        second,
        [text("pamplemousse")],
        finish="stop",
        times={"completed": rec.tick()},
        **USAGE,
    )
    rec.idle("msg_i1", "succeeded")


def test_a_turn_reads_as_one_user_one_tool_pair_and_one_reply(rec, workdir):
    src = attached(rec, workdir)
    a_turn_with_a_tool(rec, workdir)
    events = drain(src)

    assert [(e.kind, e.tool_name) for e in events] == [
        (EventKind.USER, None),
        (EventKind.TOOL_CALL, "read"),
        (EventKind.TOOL_RESULT, "read"),
        (EventKind.ASSISTANT, None),
    ]
    assert events[0].text == "read the note"
    assert events[1].paths == (EventPath(path="note.txt", mode="read"),)
    assert events[2].text == "the secret is pamplemousse"
    assert events[3].text == "pamplemousse"
    assert events[3].turn_terminal is TurnTerminal.COMPLETED
    assert events[3].usage is not None and events[3].usage.model == "anthropic/claude"
    assert drain(src) == []


def test_status_follows_the_idle_marker(rec, workdir):
    rec.user("msg_u1", "hello")
    assert asyncio.run(OpenCodeV2Source(rec.path, cwd=str(workdir)).read()).status is (
        Status.WORKING
    )
    rec.idle("msg_i1", "succeeded")
    assert asyncio.run(OpenCodeV2Source(rec.path, cwd=str(workdir)).read()).status is Status.IDLE


def test_an_interrupt_between_steps_still_ends_the_turn(rec, workdir):
    src = attached(rec, workdir)
    rec.user("msg_u1", "read the note")
    first = rec.tick()
    rec.step(
        "msg_a1",
        first,
        [tool("call_1", "completed", "/elsewhere", "done")],
        finish="tool-calls",
        times={"completed": rec.tick()},
    )
    rec.idle("msg_i1", "interrupted")
    events = drain(src)

    assert events[-1].kind is EventKind.ERROR
    assert events[-1].turn_terminal is TurnTerminal.INTERRUPTED
    assert sum(e.turn_end for e in events) == 1


def test_an_aborted_step_is_an_interrupt(rec, workdir):
    src = attached(rec, workdir)
    rec.user("msg_u1", "go")
    rec.step(
        "msg_a1",
        rec.tick(),
        [text("partial")],
        finish="error",
        error={"type": "aborted", "message": "Step interrupted"},
        times={"completed": rec.tick()},
    )
    rec.idle("msg_i1", "interrupted")
    ends = [e for e in drain(src) if e.turn_end]
    assert [(e.kind, e.turn_terminal) for e in ends] == [
        (EventKind.ERROR, TurnTerminal.INTERRUPTED)
    ]


def test_a_retryable_step_failure_waits_for_the_idle_marker(rec, workdir):
    """2.x persists a retryable step failure and continues the turn; only idle ends it."""
    src = attached(rec, workdir)
    rec.user("msg_u1", "go")
    failed = rec.tick()
    rec.step(
        "msg_a1",
        failed,
        [text("partial")],
        finish="error",
        error={"type": "api.error", "message": "retryable stream failure"},
        times={"completed": rec.tick()},
        **USAGE,
    )
    assert asyncio.run(OpenCodeV2Source(rec.path, cwd=str(workdir)).read()).status is (
        Status.WORKING
    )
    rec.step(
        "msg_a2",
        rec.tick(),
        [text("recovered")],
        finish="tool-calls",
        times={"completed": rec.tick()},
        **USAGE,
    )
    rec.idle("msg_i1", "succeeded")
    rec.user("msg_u2", "again")
    rec.step(
        "msg_a3",
        rec.tick(),
        [text("doomed")],
        finish="error",
        error={"type": "api.error", "message": "fatal stream failure"},
        times={"completed": rec.tick()},
        **USAGE,
    )
    rec.idle("msg_i2", "failed")

    events = drain(src)
    errors = [e for e in events if e.kind is EventKind.ERROR]
    assert [e.text for e in errors] == [
        "api.error: retryable stream failure",
        "api.error: fatal stream failure",
        "api.error: fatal stream failure",  # the boundary names the cause, not a generic text
    ]
    assert [e.turn_end for e in errors] == [False, False, True]
    assert [(e.kind, e.turn_terminal) for e in events if e.turn_end] == [
        (EventKind.ASSISTANT, TurnTerminal.COMPLETED),
        (EventKind.ERROR, TurnTerminal.FAILED),
    ]

    # A rewritten failed row (same id, moving time_updated) must not repeat its ERROR.
    rec.step(
        "msg_a1",
        failed,
        [text("partial")],
        at=rec.tick(),
        finish="error",
        error={"type": "api.error", "message": "retryable stream failure"},
        times={"completed": rec.tick()},
        **USAGE,
    )
    assert drain(src) == []

    cold = OpenCodeV2Source(rec.path, cwd=str(workdir))
    asyncio.run(cold.read())
    cold.commit_attachment()
    history = asyncio.run(cold.history(last_n=0)).events
    assert [(e.kind, e.text, e.turn_end, e.turn_terminal) for e in history] == [
        (e.kind, e.text, e.turn_end, e.turn_terminal) for e in events
    ]


def test_a_failed_turn_without_a_step_error_keeps_the_generic_boundary(rec, workdir):
    src = attached(rec, workdir)
    rec.user("msg_u1", "go")
    rec.step("msg_a1", rec.tick(), [text("ok")], finish="tool-calls", **USAGE)
    rec.idle("msg_i1", "failed")

    (boundary,) = [e for e in drain(src) if e.turn_end]
    assert boundary.text == "TurnFailed: OpenCode ended the turn with a failure"

    # An abort-typed step failure must not turn an authoritative `failed` outcome into an interrupt.
    rec.user("msg_u2", "again")
    rec.step(
        "msg_a2",
        rec.tick(),
        [text("cut")],
        finish="error",
        error={"type": "aborted", "message": "Step interrupted"},
        **USAGE,
    )
    rec.idle("msg_i2", "failed")

    (boundary,) = [e for e in drain(src) if e.turn_end]
    assert boundary.turn_terminal is TurnTerminal.FAILED


def test_history_and_the_live_path_agree(rec, workdir):
    src = attached(rec, workdir)
    a_turn_with_a_tool(rec, workdir)
    live = [(e.kind, e.text, e.turn_end) for e in drain(src)]
    history = asyncio.run(src.history(last_n=0)).events
    assert [(e.kind, e.text, e.turn_end) for e in history] == live


def test_attaching_mid_turn_reports_only_what_happens_after(rec, workdir):
    note = str(workdir / "note.txt")
    rec.user("msg_u1", "read both")
    first = rec.tick()
    rec.step(
        "msg_a1",
        first,
        [tool("call_1", "completed", note, "one"), tool("call_2", "running", note)],
    )
    src = attached(rec, workdir)
    rec.step(
        "msg_a1",
        first,
        [tool("call_1", "completed", note, "one"), tool("call_2", "completed", note, "two")],
        finish="tool-calls",
        times={"completed": rec.tick()},
    )
    assert [(e.kind, e.text) for e in drain(src)] == [(EventKind.TOOL_RESULT, "two")]


def write_users(rec, first: int, count: int, at: int) -> None:
    for index in range(first, first + count):
        rec.write(
            f"msg_u{index}",
            "user",
            {"time": {"created": at}, "text": f"note {index}", "files": []},
            at=at,
        )


def drain_all(src) -> tuple[list, list[int]]:
    """Every event across has_more batches, plus each batch's event count."""
    events: list = []
    sizes: list[int] = []
    while True:
        batch = asyncio.run(src.read())
        batch_events = [e for e in batch.events if not e.usage_only]
        events.extend(batch_events)
        sizes.append(len(batch_events))
        if not batch.has_more:
            return events, sizes


def test_same_timestamp_batches_drain_completely(rec, workdir):
    src = attached(rec, workdir)
    at = rec.tick()
    write_users(rec, 0, 600, at)
    events, sizes = drain_all(src)
    assert [e.text for e in events] == [f"note {index}" for index in range(600)]
    assert sizes[0] == 500 and max(sizes) <= 500
    write_users(rec, 600, 3, rec.tick())
    assert [e.text for e in drain(src)] == [f"note {i}" for i in (600, 601, 602)]


def test_attaching_after_a_large_same_timestamp_batch_replays_nothing(rec, workdir, monkeypatch):
    at = rec.tick()
    write_users(rec, 0, 600, at)
    src = OpenCodeV2Source(rec.path, cwd=str(workdir))
    sizes = []
    original = source_v2.reread_rows

    def measured(*args, **kwargs):
        rows = original(*args, **kwargs)
        sizes.append(len(rows))
        return rows

    monkeypatch.setattr(source_v2, "reread_rows", measured)
    first = asyncio.run(src.read())
    assert first.attached is None and first.has_more and not first.progressed
    assert sizes == [501]
    batch = asyncio.run(src.read())
    assert batch.attached is not None and batch.attached.skipped == 600
    assert sizes == [501, 100]
    src.commit_attachment()
    assert drain(src) == []
    write_users(rec, 600, 2, rec.tick())
    assert [e.text for e in drain(src)] == ["note 600", "note 601"]


def test_a_same_millisecond_rewrite_is_read_exactly_once(rec, workdir):
    src = attached(rec, workdir)
    rec.user("msg_u1", "hi")
    created = rec.tick()
    at = rec.tick()
    rec.step("msg_a1", created, [text("hello")], at=at)
    assert [e.kind for e in drain(src)] == [EventKind.USER]
    rec.step("msg_a1", created, [text("hello")], at=at, finish="stop", times={"completed": at})
    assert [e.turn_end for e in drain(src)] == [True]
    assert drain(src) == []


def test_a_rewrite_behind_the_forward_boundary_is_still_found(rec, workdir):
    src = attached(rec, workdir)
    note = str(workdir / "note.txt")
    rec.user("msg_u1", "read the note")
    first = rec.tick()
    rec.step("msg_a1", first, [tool("call_1", "running", note)], at=first)
    rec.user("msg_u2", "and again")
    assert [e.kind for e in drain(src)] == [EventKind.USER, EventKind.TOOL_CALL, EventKind.USER]
    rec.step("msg_a1", first, [tool("call_1", "completed", note, "done")], at=first)
    assert [(e.kind, e.text) for e in drain(src)] == [(EventKind.TOOL_RESULT, "done")]
    assert drain(src) == []


def write_steps(rec, count: int, at: int, answer: bool) -> None:
    for index in range(count):
        content = [text(f"answer {index}" if answer else "")]
        fields = {"finish": "stop", "times": {"completed": at}} if answer else {}
        rec.step(f"msg_a{index}", at, content, at=at, **fields)


def test_same_timestamp_rewrites_drain_in_order_with_bounded_reads(rec, workdir):
    src = attached(rec, workdir)
    at = rec.tick()
    write_steps(rec, 600, at, answer=False)
    _, sizes = drain_all(src)
    assert max(sizes) <= 500
    write_steps(rec, 600, at, answer=True)
    events, sizes = drain_all(src)
    assert [e.text for e in events] == [f"answer {index}" for index in range(600)]
    assert sizes == [500, 100]


def test_a_pending_reread_gets_service_under_continuous_forward_arrivals(rec, workdir):
    src = attached(rec, workdir)
    at = rec.tick()
    write_steps(rec, 600, at, answer=False)
    drain_all(src)
    write_steps(rec, 600, at, answer=True)
    answers = []
    for wave in range(2):
        write_users(rec, wave * 600, 600, rec.tick())
        batch = asyncio.run(src.read())
        assert batch.has_more and len(batch.events) <= 1000
        answers.extend(e.text for e in batch.events if e.kind is EventKind.ASSISTANT)
    assert answers == [f"answer {i}" for i in range(600)]
    remaining, _ = drain_all(src)
    assert all(e.kind is not EventKind.ASSISTANT for e in remaining)


@pytest.mark.parametrize("forward_jump", [0, 5000])
def test_a_same_ms_rewrite_after_attachment_is_not_seeded_away(rec, workdir, forward_jump):
    at = rec.tick()
    rec.step("msg_a", at, [text("")], at=at)
    src = attached(rec, workdir)
    rec.step("msg_a", at, [text("answer")], at=at, finish="stop", times={"completed": at})
    if forward_jump:
        write_users(rec, 0, 1, rec.tick(forward_jump))
    assert [e.text for e in drain(src) if e.kind is EventKind.ASSISTANT] == ["answer"]
    assert drain(src) == []


def test_attachment_pages_share_a_snapshot_while_native_writes_continue(rec, workdir):
    rec.conn.execute("PRAGMA journal_mode=WAL")
    at = rec.tick()
    write_steps(rec, 600, at, answer=False)
    src = OpenCodeV2Source(rec.path, cwd=str(workdir))
    first = asyncio.run(src.read())
    assert first.attached is None and first.has_more
    rec.step("msg_a599", at, [text("changed")], at=at, finish="stop", times={"completed": at})
    attached_batch = asyncio.run(src.read())
    assert attached_batch.attached is not None and attached_batch.has_more
    assert attached_batch.status is Status.IDLE
    src.commit_attachment()
    events, _ = drain_all(src)
    assert [e.text for e in events] == ["changed"]
    assert not src._conn.in_transaction


def test_revocation_releases_an_unfinished_attachment_snapshot(rec, workdir):
    write_users(rec, 0, 600, rec.tick())
    src = OpenCodeV2Source(rec.path, cwd=str(workdir))
    assert asyncio.run(src.read()).has_more
    assert src._conn.in_transaction
    src.revoke_attachment()
    assert not src._conn.in_transaction


def test_unchanged_polls_report_no_progress(rec, workdir):
    src = attached(rec, workdir)
    rec.user("msg_user", "old")
    assert [e.text for e in drain(src)] == ["old"]
    for _ in range(3):
        batch = asyncio.run(src.read())
        assert not batch.events and not batch.progressed and not batch.has_more


def _compaction(created: int, *, status: str = "completed", billed: bool = True) -> dict:
    data: dict = {
        "time": {"created": created},
        "status": status,
        "reason": "auto",
        "summary": "the session so far",
        "recent": "",
        "model": {"id": "claude", "providerID": "anthropic"},
    }
    if status == "failed":
        data["error"] = {
            "type": "compaction.failed",
            "message": "Compaction summary did not match the required template",
        }
    if status in ("completed", "failed") and billed:
        data["tokens"] = {
            "input": 2000,
            "output": 300,
            "reasoning": 0,
            "cache": {"read": 0, "write": 0},
        }
        data["cost"] = 0.125
    return data


def test_a_completed_compaction_accounts_its_usage_once(rec, workdir):
    src = attached(rec, workdir)
    rec.user("msg_u1", "read the note")
    created = rec.tick()
    rec.step(
        "msg_a1",
        created,
        [text("pamplemousse")],
        finish="stop",
        times={"completed": rec.tick()},
        **USAGE,
    )
    rec.idle("msg_i1", "succeeded")
    rec.write("msg_c1", "compaction", _compaction(created, status="running"))
    first = asyncio.run(src.read())
    assert [e for e in first.events if e.usage_only] == []
    assert sum(e.turn_end for e in first.events) == 1

    compaction_created = rec.tick()
    rec.write("msg_c1", "compaction", _compaction(compaction_created))
    second = asyncio.run(src.read())
    (usage_event,) = [e for e in second.events if e.usage_only]
    assert usage_event.turn_end is False and usage_event.turn_terminal is None
    assert usage_event.usage.input_tokens == 2000
    assert usage_event.usage.output_tokens == 300
    assert usage_event.usage.cost_usd == 0.125
    assert usage_event.usage.idempotency_key == "opencode:msg_c1"
    assert [e for e in second.events if not e.usage_only] == []
    (fact,) = [f for f in second.trajectory if f.native_id == "msg_c1"]
    assert fact.usage is not None
    assert (fact.usage.input_tokens, fact.usage.output_tokens) == (2000, 300)
    assert fact.usage.cost_usd == 0.125
    assert second.progressed is True

    rec.write("msg_c1", "compaction", _compaction(compaction_created))
    third = asyncio.run(src.read())
    assert third.events == [] and not third.trajectory
    all_events = [*first.events, *second.events, *third.events]
    assert sum(e.turn_end for e in all_events) == 1


def test_a_failed_compaction_accounts_its_usage_once(rec, workdir):
    src = attached(rec, workdir)
    rec.write("msg_c1", "compaction", _compaction(rec.tick(), status="running"))
    assert asyncio.run(src.read()).events == []

    failed = _compaction(rec.tick(), status="failed")
    rec.write("msg_c1", "compaction", failed)
    batch = asyncio.run(src.read())
    (usage_event,) = [e for e in batch.events if e.usage_only]
    assert usage_event.turn_end is False and usage_event.turn_terminal is None
    assert usage_event.usage.input_tokens == 2000
    assert usage_event.usage.output_tokens == 300
    assert usage_event.usage.cost_usd == 0.125
    assert usage_event.usage.idempotency_key == "opencode:msg_c1"
    assert [e for e in batch.events if e.kind is EventKind.ERROR] == []
    (fact,) = [f for f in batch.trajectory if f.native_id == "msg_c1"]
    assert fact.usage is not None
    assert (fact.usage.input_tokens, fact.usage.output_tokens) == (2000, 300)
    assert fact.usage.cost_usd == 0.125

    rec.write("msg_c1", "compaction", failed)
    rewrite = asyncio.run(src.read())
    assert rewrite.events == [] and not rewrite.trajectory

    rec.write("msg_c2", "compaction", _compaction(rec.tick(), status="failed", billed=False))
    unbilled = asyncio.run(src.read())
    assert unbilled.events == [] and not unbilled.trajectory
    seen = [*batch.events, *rewrite.events, *unbilled.events]
    assert sum(e.turn_end for e in seen) == 0
