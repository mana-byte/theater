"""opencode records projected into trajectory facts: live updates, history pages, MCP identity."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.support.opencode_recorder import (
    Recorder,
)
from tests.support.opencode_recorder import rec as shared_rec
from tests.support.shipped import OpenCodeObserver
from theater.daemon.trajectory.project import fact_to_record
from theater.harness.builtin.plugins.opencode.constants import LIVE_TRAJECTORY_STATE_LIMIT
from theater.harness.builtin.plugins.opencode.mcp import catalog_path
from theater.trajectory import ContentFormat, TrajectoryKind, TrajectoryStatus
from theater.trajectory.grouping import deterministic_record_order, merge_records

rec = shared_rec  # the shared fixture, under the name the tests ask for


def _source(rec: Recorder, workdir: Path):
    source = OpenCodeObserver(db=rec.path).open_source(cwd=str(workdir))
    attached = asyncio.run(source.read())
    assert attached.attached is not None
    source.commit_attachment()
    return source


def _opencode_source_with_mcp_catalog(
    rec: Recorder,
    workdir: Path,
    correlation_dir: Path,
    tools: dict[str, list[str]],
):
    participant_id = "participant"
    path = catalog_path(participant_id, correlation_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "servers": ["sentry"], "tools": tools}))
    source = OpenCodeObserver(db=rec.path, correlation_dir=correlation_dir).open_source_for(
        participant_id=participant_id,
        cwd=str(workdir),
    )
    attached = asyncio.run(source.read())
    assert attached.attached is not None
    source.commit_attachment()
    return source


def test_opencode_facts_upsert_running_tool_to_terminal(rec, workdir) -> None:
    source = _source(rec, workdir)
    rec.message("message-1", "assistant")
    running = {
        "id": "part-1",
        "messageID": "message-1",
        "type": "tool",
        "callID": "call-1",
        "tool": "read",
        "state": {"status": "running", "input": {"filePath": "note"}},
    }
    rec._part(running)
    first = asyncio.run(source.read())
    rec._part({**running, "state": {"status": "completed", "output": "done"}})
    second = asyncio.run(source.read())

    first_call = next(fact for fact in first.trajectory if fact.kind is TrajectoryKind.TOOL_CALL)
    second_call = next(fact for fact in second.trajectory if fact.kind is TrajectoryKind.TOOL_CALL)
    result = next(fact for fact in second.trajectory if fact.kind is TrajectoryKind.TOOL_RESULT)
    assert first_call.native_id == second_call.native_id == "call-1"
    assert first.trajectory_events == ()
    assert first.events[0].raw_index == 1
    assert first_call.status is TrajectoryStatus.RUNNING
    assert second_call.status is TrajectoryStatus.COMPLETED
    assert second_call.revision > first_call.revision
    assert first_call.request_id == second_call.request_id == "opencode:message-1"
    assert result.call_id == second_call.call_id == "call-1"
    assert result.request_id == "opencode:message-1"
    assert result.details[0].format is ContentFormat.TEXT

    rec._part({**running, "state": {"status": "completed", "output": "done"}})
    duplicate = asyncio.run(source.read())
    assert duplicate.trajectory == ()


def test_opencode_marks_json_tool_results_live_and_in_history(rec, workdir) -> None:
    source = _source(rec, workdir)
    message = rec.message("message-1", "assistant")
    rec._part(
        {
            "id": "part-1",
            "messageID": message["id"],
            "type": "tool",
            "callID": "call-1",
            "tool": "external_call",
            "state": {"status": "completed", "output": '{"ok":true,"items":[1,2]}'},
        }
    )

    live = asyncio.run(source.read())
    history = asyncio.run(source.history_page(limit=10))
    results = [
        next(fact for fact in facts if fact.kind is TrajectoryKind.TOOL_RESULT)
        for facts in (live.trajectory, history.trajectory)
    ]

    assert [result.details[0].format for result in results] == [
        ContentFormat.JSON,
        ContentFormat.JSON,
    ]
    assert all(result.details[0].preview.text == '{"ok":true,"items":[1,2]}' for result in results)


@pytest.mark.parametrize("status,output", [("completed", '{"ok":true}'), ("error", "failed")])
def test_opencode_completed_tool_fields_match_live_and_history_except_step_id(
    rec, workdir, status, output
) -> None:
    source = _source(rec, workdir)
    message = rec.message("message-1", "assistant")
    state = {"status": status, "input": {"path": "note"}}
    state["output" if status == "completed" else "error"] = output
    rec._part(
        {
            "id": "part-1",
            "messageID": message["id"],
            "stepID": "step-1",
            "type": "tool",
            "callID": "call-1",
            "parentCallID": "parent-1",
            "tool": "theater_send",
            "state": state,
        }
    )

    live = asyncio.run(source.read())
    history = asyncio.run(source.history_page(limit=10))
    for kind in (TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT):
        live_fact = next(fact for fact in live.trajectory if fact.kind is kind)
        stored_fact = next(fact for fact in history.trajectory if fact.kind is kind)
        assert live_fact.step_id == "step-1"
        assert stored_fact.step_id is None
        assert replace(live_fact, step_id=None) == stored_fact


def test_opencode_live_trajectory_state_is_bounded(rec, workdir) -> None:
    source = _source(rec, workdir)
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


def test_opencode_live_and_history_revisions_share_coordinates(rec, workdir) -> None:
    source = _source(rec, workdir)
    message = rec.message("message-1", "assistant")
    running = {
        "id": "part-1",
        "messageID": message["id"],
        "type": "tool",
        "callID": "call-1",
        "tool": "read",
        "state": {"status": "running"},
    }
    rec._part(running)

    live = asyncio.run(source.read())
    history = asyncio.run(source.history_page(limit=10))
    live_call = next(fact for fact in live.trajectory if fact.kind is TrajectoryKind.TOOL_CALL)
    stored_call = next(fact for fact in history.trajectory if fact.kind is TrajectoryKind.TOOL_CALL)

    assert live_call.raw_index == stored_call.raw_index
    assert live_call.revision == stored_call.revision
    assert live_call.request_id == stored_call.request_id == "opencode:message-1"

    rec._part({**running, "state": {"status": "completed", "output": "done"}})
    completed = asyncio.run(source.read())
    completed_call = next(
        fact for fact in completed.trajectory if fact.kind is TrajectoryKind.TOOL_CALL
    )
    assert completed_call.revision > stored_call.revision


async def test_opencode_part_order_survives_live_updates_tool_completion_and_message_finish(
    rec, workdir
):
    message = rec.message("message-1", "assistant")
    parts = [
        {"id": "part-z", "type": "reasoning", "text": "reasoning"},
        {
            "id": "part-y",
            "type": "tool",
            "callID": "call-a",
            "tool": "read",
            "state": {"status": "running"},
        },
        {"id": "part-x", "type": "text", "text": "first"},
        {"id": "part-w", "type": "text", "text": "second"},
        {"id": "part-v", "type": "context", "text": "context"},
        {"id": "part-u", "type": "system", "text": "system"},
    ]
    for part in parts:
        part["messageID"] = message["id"]
        rec._part(part)
    source = OpenCodeObserver(db=rec.path).open_source(cwd=str(workdir))
    assert (await source.read()).attached is not None
    source.commit_attachment()
    history = await source.history_page(limit=20)
    initial = {fact.native_id: (fact.raw_index, fact.event_ordinal) for fact in history.trajectory}

    for part in reversed(parts):
        rec._part(
            {
                **part,
                "text": "updated",
                **(
                    {"state": {"status": "completed", "output": "done"}}
                    if part["type"] == "tool"
                    else {}
                ),
            }
        )
    rec.finish(message, "stop")
    live = await source.read()
    for fact in live.trajectory:
        if fact.kind is not TrajectoryKind.TOOL_RESULT:
            assert (fact.raw_index, fact.event_ordinal) == initial[fact.native_id]
    latest = await source.history_page(limit=20)
    assert {
        fact.native_id: (fact.raw_index, fact.event_ordinal)
        for fact in latest.trajectory
        if fact.kind is not TrajectoryKind.TOOL_RESULT
    } == initial
    records = deterministic_record_order(
        merge_records(
            [
                fact_to_record(fact, participant_id="p", source_epoch="epoch")
                for fact in history.trajectory
            ],
            [
                fact_to_record(fact, participant_id="p", source_epoch="epoch")
                for fact in live.trajectory
            ],
        )
    )
    assert [record.event_ordinal for record in records] == [0, 2, 3, 4, 6, 8, 10]


def test_opencode_extracts_theater_mcp_identity_live_and_from_history(rec, workdir) -> None:
    source = _source(rec, workdir)
    message = rec.message("message-1", "assistant")
    running = {
        "id": "part-1",
        "messageID": message["id"],
        "type": "tool",
        "callID": "call-1",
        "tool": "theater_send",
        "state": {"status": "running", "input": {}},
    }
    rec._part(running)
    live = asyncio.run(source.read())
    rec._part({**running, "state": {"status": "completed", "output": "sent"}})
    completed = asyncio.run(source.read())
    history = asyncio.run(source.history_page(limit=10))

    facts = (*live.trajectory, *completed.trajectory, *history.trajectory)
    mcp_facts = [
        fact
        for fact in facts
        if fact.kind in {TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT}
    ]
    assert mcp_facts
    assert {(fact.mcp_server, fact.mcp_tool) for fact in mcp_facts} == {("theater", "send")}


def test_opencode_extracts_observed_external_mcp_identity_live_and_from_history(
    rec, workdir, tmp_path
) -> None:
    source = _opencode_source_with_mcp_catalog(
        rec,
        workdir,
        tmp_path / "correlation",
        {"sentry_find_organizations": ["sentry", "find_organizations"]},
    )
    message = rec.message("message-1", "assistant")
    running = {
        "id": "part-1",
        "messageID": message["id"],
        "type": "tool",
        "callID": "call-1",
        "tool": "sentry_find_organizations",
        "state": {"status": "running", "input": {}},
    }
    rec._part(running)
    live = asyncio.run(source.read())
    rec._part({**running, "state": {"status": "completed", "output": "found"}})
    completed = asyncio.run(source.read())
    history = asyncio.run(source.history_page(limit=10))

    facts = (*live.trajectory, *completed.trajectory, *history.trajectory)
    mcp_facts = [
        fact
        for fact in facts
        if fact.kind in {TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT}
    ]
    assert mcp_facts
    assert {(fact.mcp_server, fact.mcp_tool) for fact in mcp_facts} == {
        ("sentry", "find_organizations")
    }


def test_opencode_reclassifies_a_live_tool_when_the_catalog_arrives(rec, workdir, tmp_path) -> None:
    correlation_dir = tmp_path / "correlation"
    source = _opencode_source_with_mcp_catalog(rec, workdir, correlation_dir, {})
    message = rec.message("message-1", "assistant")
    rec._part(
        {
            "id": "part-1",
            "messageID": message["id"],
            "type": "tool",
            "callID": "call-1",
            "tool": "sentry_find_organizations",
            "state": {"status": "completed", "input": {}, "output": "found"},
        }
    )
    first = asyncio.run(source.read())
    first_tools = {
        fact.kind: fact
        for fact in first.trajectory
        if fact.kind in {TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT}
    }
    assert set(first_tools) == {TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT}
    assert all(fact.mcp_server is None for fact in first_tools.values())

    catalog_path("participant", correlation_dir).write_text(
        json.dumps(
            {
                "version": 1,
                "servers": ["sentry"],
                "tools": {"sentry_find_organizations": ["sentry", "find_organizations"]},
            }
        )
    )
    refreshed = asyncio.run(source.read())
    refreshed_tools = {
        fact.kind: fact
        for fact in refreshed.trajectory
        if fact.kind in {TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT}
    }

    assert refreshed.progressed is False
    assert set(refreshed_tools) == {TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT}
    for kind, fact in refreshed_tools.items():
        assert fact.revision > first_tools[kind].revision
        assert (fact.mcp_server, fact.mcp_tool) == ("sentry", "find_organizations")


def test_opencode_history_page_is_keyset_and_does_not_move_live_cursor(rec, workdir) -> None:
    source = _source(rec, workdir)
    for index in range(5):
        message = rec.message(f"message-{index}", "user")
        rec.user_text(message["id"], f"part-{index}", f"text-{index}")
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


def test_opencode_history_cursor_survives_source_restart_and_rejects_mutated_boundary(
    rec, workdir
) -> None:
    source = _source(rec, workdir)
    first = rec.message("message-1", "user")
    rec.user_text(first["id"], "part-1", "text-1")
    second = rec.message("message-2", "user")
    rec.user_text(second["id"], "part-2", "text-2")
    page = asyncio.run(source.history_page(limit=1))
    assert page.older_cursor is not None

    restarted = OpenCodeObserver(db=rec.path).open_source(cwd=str(workdir), session_id="ses_one")
    valid = asyncio.run(restarted.history_page(before=page.older_cursor, limit=1))
    assert valid.error_code is None

    rec.conn.execute(
        "UPDATE message SET data = ? WHERE id = ?", (json.dumps({"id": "message-2"}), "message-2")
    )
    rec.conn.commit()
    invalid = asyncio.run(source.history_page(before=page.older_cursor, limit=1))
    assert invalid.error_code == "history_cursor_invalid"
    assert invalid.older_cursor is None


def test_opencode_history_page_rejects_one_message_with_too_many_outputs(rec, workdir) -> None:
    source = _source(rec, workdir)
    message = rec.message("message-1", "assistant")
    for index in range(3):
        rec.user_text(message["id"], f"part-{index}", f"text-{index}")

    page = asyncio.run(source.history_page(limit=2))
    assert page.error_code == "history_record_too_large"
    assert page.older_cursor is None
    assert page.has_older is False
