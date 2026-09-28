"""OpenCode 2.x drain: a same-timestamp batch larger than the drain limit never stalls."""

from __future__ import annotations

import asyncio

import pytest
from test_harness_opencode_v2 import RecorderV2, attached, drain, text, tool

from theater.harness import EventKind
from theater.harness.builtin.plugins.opencode.source_v2 import OpenCodeV2Source


@pytest.fixture
def workdir(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return d


@pytest.fixture
def rec(tmp_path, workdir):
    r = RecorderV2(tmp_path / "opencode-v2.db", "ses_one", str(workdir))
    yield r
    r.conn.close()


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


def test_attaching_after_a_large_same_timestamp_batch_replays_nothing(rec, workdir):
    at = rec.tick()
    write_users(rec, 0, 600, at)
    src = OpenCodeV2Source(rec.path, cwd=str(workdir))
    batch = asyncio.run(src.read())
    assert batch.attached is not None and batch.attached.skipped == 600
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
