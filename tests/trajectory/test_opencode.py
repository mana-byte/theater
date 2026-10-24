"""OpenCode projection updates, history pages and MCP identities produce stable trajectory facts."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.support.opencode_recorder import attached, text, tool
from tests.support.opencode_recorder import rec as shared_rec
from tests.support.shipped import OpenCodeObserver
from theater.daemon.trajectory.project import fact_to_record
from theater.harness.builtin.plugins.opencode.constants import LIVE_TRAJECTORY_STATE_LIMIT
from theater.harness.builtin.plugins.opencode.mcp import catalog_path
from theater.trajectory import ContentFormat, TrajectoryKind, TrajectoryStatus
from theater.trajectory.grouping import deterministic_record_order, merge_records

rec = shared_rec


@pytest.fixture
def source(rec, workdir):
    src = attached(rec, workdir)
    yield src
    asyncio.run(src.aclose())


@pytest.fixture
def catalog_source(rec, workdir, tmp_path):
    directory = tmp_path / "correlation"
    path = catalog_path("participant", directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "servers": ["sentry"], "tools": {}}))
    src = OpenCodeObserver(db=rec.path, correlation_dir=directory).open_source_for(
        participant_id="participant",
        cwd=str(workdir),
    )
    assert asyncio.run(src.read()).attached is not None
    src.commit_attachment()
    yield src, path
    asyncio.run(src.aclose())


def _fact(batch, kind):
    return next(fact for fact in batch.trajectory if fact.kind is kind)


def test_tool_completion_revises_the_same_call_once(rec, source):
    created = rec.tick()
    rec.step("message-1", created, [tool("call-1", "running", "note")])
    first = asyncio.run(source.read())
    rec.step("message-1", created, [tool("call-1", "completed", "note", "done")])
    second = asyncio.run(source.read())

    running, completed = (_fact(batch, TrajectoryKind.TOOL_CALL) for batch in (first, second))
    result = _fact(second, TrajectoryKind.TOOL_RESULT)
    assert running.native_id == completed.native_id == "call-1"
    assert running.status is TrajectoryStatus.RUNNING
    assert completed.status is TrajectoryStatus.COMPLETED
    assert completed.revision > running.revision
    assert running.request_id == completed.request_id == "opencode:message-1"
    assert result.call_id == completed.call_id == "call-1"
    assert result.request_id == "opencode:message-1"
    assert result.details[0].format is ContentFormat.TEXT
    assert first.trajectory_events == ()

    rec.step("message-1", created, [tool("call-1", "completed", "note", "done")])
    assert asyncio.run(source.read()).trajectory == ()


@pytest.mark.parametrize("status,output", [("completed", '{"ok":true}'), ("error", "failed")])
def test_live_tool_fields_match_history(rec, source, status, output):
    content = tool("call-1", status, "note", output)
    if status == "error":
        content["state"]["error"] = {"type": "failure", "message": output}
    rec.step("message-1", rec.tick(), [content])
    live = asyncio.run(source.read())
    history = asyncio.run(source.history_page(limit=10))
    for kind in (TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT):
        assert _fact(live, kind) == _fact(history, kind)
    result = _fact(live, TrajectoryKind.TOOL_RESULT)
    expected_format = ContentFormat.JSON if status == "completed" else ContentFormat.TEXT
    assert result.details[0].format is expected_format
    assert result.details[0].preview.text == output


def test_live_trajectory_state_is_bounded(source):
    for index in range(LIVE_TRAJECTORY_STATE_LIMIT + 1):
        source._live_fact(
            kind=TrajectoryKind.SYSTEM,
            summary=str(index),
            status=TrajectoryStatus.COMPLETED,
            native_id=f"fact-{index}",
            fallback_id=None,
            raw_index=index,
            event_ordinal=0,
        )
    assert len(source._trajectory_state) == LIVE_TRAJECTORY_STATE_LIMIT
    assert "fact-0" not in source._trajectory_state
    assert f"fact-{LIVE_TRAJECTORY_STATE_LIMIT}" in source._trajectory_state


def test_running_and_completed_calls_share_history_coordinates(rec, source):
    created = rec.tick()
    rec.step("message-1", created, [tool("call-1", "running", "note")])
    live = asyncio.run(source.read())
    history = asyncio.run(source.history_page(limit=10))
    current, stored = (_fact(batch, TrajectoryKind.TOOL_CALL) for batch in (live, history))
    assert current.raw_index == stored.raw_index
    assert current.revision == stored.revision
    assert current.request_id == stored.request_id == "opencode:message-1"

    rec.step("message-1", created, [tool("call-1", "completed", "note", "done")])
    assert _fact(asyncio.run(source.read()), TrajectoryKind.TOOL_CALL).revision > stored.revision


def test_part_order_survives_rewrites_and_tool_completion(rec, source):
    created = rec.tick()
    content = [
        {"type": "reasoning", "text": "reasoning"},
        tool("call-1", "running", "note"),
        text("first"),
        text("second"),
    ]
    rec.step("message-1", created, content)
    history = asyncio.run(source.history_page(limit=20))
    initial = {fact.native_id: (fact.raw_index, fact.event_ordinal) for fact in history.trajectory}
    rec.step(
        "message-1",
        created,
        [content[0], tool("call-1", "completed", "note", "done"), text("updated"), content[3]],
        finish="stop",
        times={"completed": rec.tick()},
    )
    live = asyncio.run(source.read())
    for fact in live.trajectory:
        if fact.kind is not TrajectoryKind.TOOL_RESULT:
            assert (fact.raw_index, fact.event_ordinal) == initial[fact.native_id]
    latest = asyncio.run(source.history_page(limit=20))
    assert {
        fact.native_id: (fact.raw_index, fact.event_ordinal)
        for fact in latest.trajectory
        if fact.kind is not TrajectoryKind.TOOL_RESULT
    } == initial
    records = deterministic_record_order(
        merge_records(
            *[
                [
                    fact_to_record(fact, participant_id="p", source_epoch="epoch")
                    for fact in batch.trajectory
                ]
                for batch in (history, live)
            ]
        )
    )
    assert [record.event_ordinal for record in records] == [0, 2, 3, 4, 6]


@pytest.mark.parametrize(
    "name,server,method",
    [
        ("theater_send", "theater", "send"),
        ("sentry_find_organizations", "sentry", "find_organizations"),
    ],
)
def test_mcp_identity_matches_live_and_history(rec, catalog_source, name, server, method):
    source, path = catalog_source
    path.write_text(
        json.dumps({"version": 1, "servers": [server], "tools": {name: [server, method]}})
    )
    content = {**tool("call-1", "completed", "note", "done"), "name": name}
    rec.step("message-1", rec.tick(), [content])
    live = asyncio.run(source.read())
    history = asyncio.run(source.history_page(limit=10))
    for batch in (live, history):
        for kind in (TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT):
            fact = _fact(batch, kind)
            assert (fact.mcp_server, fact.mcp_tool) == (server, method)


def test_catalog_arrival_reclassifies_live_tools_without_a_new_turn(rec, catalog_source):
    source, path = catalog_source
    rec.step(
        "message-1",
        rec.tick(),
        [
            {
                **tool("call-1", "completed", "note", "found"),
                "name": "sentry_find_organizations",
            }
        ],
    )
    first = asyncio.run(source.read())
    assert all(fact.mcp_server is None for fact in first.trajectory)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "servers": ["sentry"],
                "tools": {"sentry_find_organizations": ["sentry", "find_organizations"]},
            }
        )
    )
    refreshed = asyncio.run(source.read())
    assert refreshed.progressed is False
    for kind in (TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT):
        earlier, later = (_fact(batch, kind) for batch in (first, refreshed))
        assert later.revision > earlier.revision
        assert (later.mcp_server, later.mcp_tool) == ("sentry", "find_organizations")


def test_history_pages_are_keyset_and_do_not_move_live_cursor(rec, source):
    for index in range(5):
        rec.user(f"message-{index}", f"text-{index}")
    before = source._cursor
    newest = asyncio.run(source.history_page(limit=2))
    assert newest.error_code is None
    assert newest.trajectory_events == ()
    assert [event.text for event in newest.events] == ["text-3", "text-4"]
    assert source._cursor == before
    assert newest.older_cursor is not None
    older = asyncio.run(source.history_page(before=newest.older_cursor, limit=2))
    assert older.error_code is None
    assert [event.text for event in older.events] == ["text-1", "text-2"]
    assert max(event.raw_index for event in older.events) < min(
        event.raw_index for event in newest.events
    )
    assert source._cursor == before


def test_history_cursor_survives_restart_and_rejects_a_mutated_boundary(rec, source, workdir):
    rec.user("message-1", "text-1")
    rec.user("message-2", "text-2")
    page = asyncio.run(source.history_page(limit=1))
    assert page.older_cursor is not None
    restarted = OpenCodeObserver(db=rec.path).open_source(cwd=str(workdir), session_id=rec.sid)
    try:
        valid = asyncio.run(restarted.history_page(before=page.older_cursor, limit=1))
        assert valid.error_code is None
    finally:
        asyncio.run(restarted.aclose())
    rec.conn.execute(
        "UPDATE session_message SET data = ? WHERE id = ?",
        (json.dumps({"time": {"created": rec.tick()}, "text": "changed"}), "message-2"),
    )
    rec.conn.commit()
    invalid = asyncio.run(source.history_page(before=page.older_cursor, limit=1))
    assert invalid.error_code == "history_cursor_invalid"
    assert invalid.older_cursor is None


def test_history_rejects_one_message_with_too_many_outputs(rec, source):
    rec.step("message-1", rec.tick(), [text(f"text-{index}") for index in range(3)])
    page = asyncio.run(source.history_page(limit=2))
    assert page.error_code == "history_record_too_large"
    assert page.older_cursor is None
    assert page.has_older is False
