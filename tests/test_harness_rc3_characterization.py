"""Public entrypoint contracts retained through the RC3 adapter split."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from theater.harness import EventKind
from theater.harness.builtin.plugins.claude.observer import ClaudeCodeObserver
from theater.harness.builtin.plugins.codex import CodexObserver
from theater.harness.builtin.plugins.opencode import OpenCodeObserver
from theater.harness.builtin.plugins.vibe import VibeObserver
from theater.trajectory.enums import TrajectoryKind, TrajectoryStatus

FIXTURES = Path(__file__).parent / "fixtures"


def _event_shape(events):
    return [
        (event.kind, event.text, event.tool_name, event.turn_end, event.turn_id) for event in events
    ]


def _fact_shape(facts):
    return [
        (
            fact.kind,
            fact.native_id,
            fact.status,
            fact.turn_id,
            fact.call_id,
            fact.parent_call_id,
        )
        for fact in facts
    ]


@pytest.mark.parametrize(
    (
        "observer_type,fixture,user_text,user_turn,tool_name,tool_turn,call_native,result_native,call_id"
    ),
    [
        (
            ClaudeCodeObserver,
            "trajectory_claude.jsonl",
            "inspect the file",
            None,
            "Read",
            "msg-1",
            "tool-1",
            "result-1",
            "tool-1",
        ),
        (
            CodexObserver,
            "trajectory_codex.jsonl",
            "make the change",
            None,
            "exec",
            None,
            "item-call-1",
            "item-result-1",
            "call-1",
        ),
        (
            VibeObserver,
            "vibe_messages.jsonl",
            "<user content 10677 chars>",
            "18a38665-717b-4edc-8725-d9fbc1d6ba7e",
            "read_file",
            "18a38665-717b-4edc-8725-d9fbc1d6ba7e",
            "chatcmpl-tool-ad6d248698aa1cfe",
            "chatcmpl-tool-ad6d248698aa1cfe:result",
            "chatcmpl-tool-ad6d248698aa1cfe",
        ),
    ],
)
def test_transcript_entrypoints_preserve_public_parse_record_shape(
    observer_type,
    fixture,
    user_text,
    user_turn,
    tool_name,
    tool_turn,
    call_native,
    result_native,
    call_id,
):
    observer = observer_type()
    parsed = [
        observer.parse_record(line, index, clip_text=False)
        for index, line in enumerate((FIXTURES / fixture).read_text().splitlines())
    ]
    events = [event for record in parsed for event in record.events]
    facts = [fact for record in parsed for fact in record.trajectory]
    user = next(event for event in events if event.kind is EventKind.USER)
    tool = next(event for event in events if event.kind is EventKind.TOOL_CALL)
    call = next(fact for fact in facts if fact.kind is TrajectoryKind.TOOL_CALL)
    result = next(fact for fact in facts if fact.kind is TrajectoryKind.TOOL_RESULT)

    assert (user.text, user.turn_id) == (user_text, user_turn)
    assert (tool.tool_name, tool.turn_id) == (tool_name, tool_turn)
    assert (call.native_id, call.call_id) == (call_native, call_id)
    assert (result.native_id, result.call_id, result.status) == (
        result_native,
        call_id,
        TrajectoryStatus.COMPLETED,
    )


async def test_opencode_entrypoint_preserves_public_source_shape(tmp_path: Path):
    database = tmp_path / "opencode.db"
    workdir = tmp_path / "work"
    workdir.mkdir()
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY, parent_id TEXT,
            directory TEXT, time_created INTEGER
        );
        CREATE TABLE event (
            id INTEGER PRIMARY KEY AUTOINCREMENT, aggregate_id TEXT,
            seq INTEGER, type TEXT, data TEXT
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
            time_updated INTEGER, data TEXT
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, time_updated INTEGER, data TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO session (id, parent_id, directory, time_created) VALUES (?, NULL, ?, ?)",
        ("session-1", str(workdir.resolve()), 1000),
    )
    connection.commit()
    source = OpenCodeObserver(db=database).open_source(cwd=str(workdir))
    try:
        attached = await source.read()
        assert attached.attached is not None
        source.commit_attachment()
        events = [
            (
                "message.updated.1",
                {"info": {"id": "user-1", "role": "user", "time": {"created": 1000}}},
            ),
            (
                "message.part.updated.1",
                {
                    "part": {
                        "id": "user-part",
                        "messageID": "user-1",
                        "type": "text",
                        "text": "ask",
                    },
                    "time": 1010,
                },
            ),
            (
                "message.updated.1",
                {
                    "info": {
                        "id": "assistant-1",
                        "role": "assistant",
                        "time": {"created": 1015},
                    }
                },
            ),
            (
                "message.part.updated.1",
                {
                    "part": {
                        "id": "tool-part",
                        "messageID": "assistant-1",
                        "type": "tool",
                        "callID": "call-1",
                        "tool": "read",
                        "state": {"status": "completed", "input": {}, "output": "found"},
                    },
                    "time": 1020,
                },
            ),
            (
                "message.updated.1",
                {
                    "info": {
                        "id": "assistant-1",
                        "role": "assistant",
                        "finish": "stop",
                        "time": {"created": 1015, "completed": 1030},
                    }
                },
            ),
        ]
        connection.executemany(
            "INSERT INTO event (aggregate_id, seq, type, data) VALUES (?, ?, ?, ?)",
            [
                ("session-1", index, kind, json.dumps(payload))
                for index, (kind, payload) in enumerate(events)
            ],
        )
        connection.commit()

        batch = await source.read()
        call = next(fact for fact in batch.trajectory if fact.kind is TrajectoryKind.TOOL_CALL)
        result = next(fact for fact in batch.trajectory if fact.kind is TrajectoryKind.TOOL_RESULT)
        assert _event_shape(batch.events) == [
            (EventKind.USER, "ask", None, False, None),
            (EventKind.TOOL_CALL, "", "read", False, None),
            (EventKind.TOOL_RESULT, "found", "read", False, None),
            (EventKind.ASSISTANT, "", None, True, "assistant-1"),
        ]
        assert (call.native_id, result.native_id, result.call_id) == (
            "call-1",
            "call-1:result",
            "call-1",
        )
    finally:
        await source.aclose()
        connection.close()


def _transcript_case(name: str, tmp_path: Path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    if name == "claude":
        transcript = tmp_path / name / "project" / "session-1.jsonl"
        transcript.parent.mkdir(parents=True)
        source = ClaudeCodeObserver(root=transcript.parent.parent).open_source(
            cwd=str(workdir), session_id="session-1"
        )
        records = [
            {"type": "user", "message": {"role": "user", "content": "ask"}},
            {
                "type": "assistant",
                "message": {
                    "id": "message-1",
                    "content": [{"type": "text", "text": "answer"}],
                    "stop_reason": "end_turn",
                },
            },
        ]
    elif name == "codex":
        transcript = (
            tmp_path / name / "2026" / "01" / "01" / "rollout-2026-01-01T00-00-00-session-1.jsonl"
        )
        transcript.parent.mkdir(parents=True)
        source = CodexObserver(root=transcript.parents[3]).open_source(
            cwd=str(workdir), session_id="session-1"
        )
        records = [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "ask"}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-1",
                    "last_agent_message": "answer",
                },
            },
        ]
    else:
        session = tmp_path / name / "session_20260827_120000_deadbeef"
        session.mkdir(parents=True)
        transcript = session / "messages.jsonl"
        (session / "meta.json").write_text(
            json.dumps(
                {"session_id": "deadbeef-full", "environment": {"working_directory": str(workdir)}}
            )
        )
        source = VibeObserver(root=session.parent).open_source(cwd=str(workdir))
        records = [
            {"role": "user", "content": "ask", "message_id": "turn-1"},
            {"role": "assistant", "content": "answer", "message_id": "message-1"},
        ]
    transcript.write_text("")
    return source, transcript, records


@pytest.mark.parametrize("name", ["claude", "codex", "vibe"])
async def test_transcript_entrypoints_keep_live_and_history_in_sync(name: str, tmp_path: Path):
    source, transcript, records = _transcript_case(name, tmp_path)
    try:
        attached = await source.read()
        assert attached.attached is not None
        source.commit_attachment()
        with transcript.open("a") as output:
            output.writelines(f"{json.dumps(record)}\n" for record in records)

        batch = await source.read()
        history = await source.history(last_n=0)
        page = await source.history_page(limit=10)
        assert _event_shape(batch.events) == _event_shape(history.events)
        assert _fact_shape(batch.trajectory) == _fact_shape(page.trajectory)
    finally:
        await source.aclose()
