from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from shipped import OpenCodeObserver, VibeObserver
from test_harness_opencode import Recorder

from theater.harness import EventKind
from theater.trajectory import TrajectoryKind, TrajectoryStatus

FIXTURE = Path(__file__).parent / "fixtures" / "vibe_messages.jsonl"


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def rec(tmp_path: Path, workdir: Path):
    recorder = Recorder(tmp_path / "opencode-stable.db", "ses_one", str(workdir))
    yield recorder
    recorder.conn.close()


def _source(rec: Recorder, workdir: Path):
    source = OpenCodeObserver(db=rec.path).open_source(cwd=str(workdir))
    attached = asyncio.run(source.read())
    assert attached.attached is not None
    source.commit_attachment()
    return source


def test_vibe_parse_record_preserves_control_events_and_source_timestamps() -> None:
    observer = VibeObserver()
    for index, line in enumerate(FIXTURE.read_text().splitlines()):
        parsed = observer.parse_record(line, index)
        assert list(parsed.events) == observer.parse(line, index)
        assert all(event.ts is None for event in parsed.events)


def test_vibe_facts_keep_ids_reasoning_and_tool_pairing() -> None:
    observer = VibeObserver()
    records = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    facts = [
        fact
        for index, record in enumerate(records)
        for fact in observer.parse_record(json.dumps(record), index).facts
    ]

    assert any(
        fact.kind is TrajectoryKind.REASONING
        and fact.native_id == "25417c9d-d468-4619-a15b-a499dff0cea5"
        for fact in facts
    )
    call = next(fact for fact in facts if fact.kind is TrajectoryKind.TOOL_CALL)
    result = next(fact for fact in facts if fact.kind is TrajectoryKind.TOOL_RESULT)
    assert call.native_id == call.call_id == "chatcmpl-tool-ad6d248698aa1cfe"
    assert call.status is TrajectoryStatus.UNKNOWN
    assert result.call_id == call.call_id
    assert result.native_id == f"{call.call_id}:result"
    assert call.details[0].name == "arguments"


def test_vibe_facts_bound_malformed_large_content_without_timing() -> None:
    observer = VibeObserver()
    record = {
        "role": "assistant",
        "message_id": "m1",
        "content": "\ud800" + "x" * 50_000,
        "reasoning_content": "r" * 50_000,
        "tool_calls": [
            {
                "id": "c1",
                "function": {"name": "read_file", "arguments": "{" + "x" * 50_000},
            }
        ],
    }
    parsed = observer.parse_record(json.dumps(record), 4)

    assert parsed.events[0].kind is EventKind.ASSISTANT
    assert all(fact.timing is None for fact in parsed.facts)
    assert all(len(fact.summary.encode()) <= 16 * 1024 for fact in parsed.facts)
    assert all(
        detail.preview.encoded_bytes <= 16 * 1024
        for fact in parsed.facts
        for detail in fact.details
    )


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
    assert result.call_id == second_call.call_id == "call-1"

    rec._part({**running, "state": {"status": "completed", "output": "done"}})
    duplicate = asyncio.run(source.read())
    assert duplicate.trajectory == ()


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
    stored_call = next(
        fact for fact in history.trajectory if fact.kind is TrajectoryKind.TOOL_CALL
    )

    assert live_call.raw_index == stored_call.raw_index
    assert live_call.revision == stored_call.revision

    rec._part({**running, "state": {"status": "completed", "output": "done"}})
    completed = asyncio.run(source.read())
    completed_call = next(
        fact for fact in completed.trajectory if fact.kind is TrajectoryKind.TOOL_CALL
    )
    assert completed_call.revision > stored_call.revision


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
