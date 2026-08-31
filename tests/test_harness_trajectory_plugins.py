from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest
from shipped import ClaudeCodeObserver, CodexObserver, OpenCodeObserver, VibeObserver
from test_harness_opencode import Recorder

from theater.harness import EventKind
from theater.harness.builtin.plugins.opencode.constants import LIVE_TRAJECTORY_STATE_LIMIT
from theater.harness.builtin.plugins.opencode.mcp import catalog_path
from theater.harness.builtin.plugins.vibe.trajectory import _vibe_named_mcp_identity
from theater.trajectory import ContentFormat, TimingProvenance, TrajectoryKind, TrajectoryStatus
from theater.trajectory.capabilities import TrajectoryFeature

FIXTURE = Path(__file__).parent / "fixtures" / "vibe_messages.jsonl"
CODEX_FIXTURE = Path(__file__).parent / "fixtures" / "trajectory_codex.jsonl"


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
    assert {fact.turn_id for fact in facts} == {records[0]["message_id"]}


def test_vibe_facts_parse_current_tool_result_shape(workdir: Path) -> None:
    observer = VibeObserver()
    observer._cwd = str(workdir)
    records = [
        {"role": "user", "content": "inspect", "message_id": "turn-1"},
        {
            "role": "assistant",
            "message_id": "assistant-1",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"file_path": str(workdir / "note.txt")}),
                    },
                    "presentation": {
                        "kind": "file_read",
                        "display": {"message": "Read note.txt"},
                    },
                }
            ],
        },
        {
            "role": "tool",
            "name": "read_file",
            "tool_call_id": "call-1",
            "content": "file contents",
            "tool_result": {
                "output": {"content": "file contents"},
                "duration": 0.125,
                "cancelled": False,
                "presentation": {
                    "kind": "file_read",
                    "display": {"success": True, "message": "Read note.txt"},
                },
            },
        },
        {"role": "assistant", "content": "done", "message_id": "assistant-2"},
    ]
    parsed = [
        observer.parse_record(json.dumps(record), index) for index, record in enumerate(records)
    ]
    facts = [fact for item in parsed for fact in item.trajectory]
    call = next(fact for fact in facts if fact.kind is TrajectoryKind.TOOL_CALL)
    result = next(fact for fact in facts if fact.kind is TrajectoryKind.TOOL_RESULT)

    assert all(item.trajectory_events == () for item in parsed)
    assert {fact.turn_id for fact in facts} == {"turn-1"}
    assert call.summary == "Read note.txt"
    assert any(
        detail.format is ContentFormat.PATH and detail.preview.text == "note.txt"
        for detail in call.details
    )
    assert result.summary == "Read note.txt"
    assert result.status is TrajectoryStatus.COMPLETED
    assert result.timing is not None
    assert result.timing.duration_ms == 125
    assert result.timing.provenance is TimingProvenance.SOURCE
    result_detail = next(detail for detail in result.details if detail.name == "result")
    assert result_detail.format is ContentFormat.JSON
    assert result_detail.preview.text == '{"content": "file contents"}'


def test_vibe_recognizes_tagged_tool_errors_and_cancellation() -> None:
    observer = VibeObserver()
    observer.parse_record(json.dumps({"role": "user", "content": "run", "message_id": "turn-1"}), 0)
    failed = observer.parse_record(
        json.dumps(
            {
                "role": "tool",
                "name": "bash",
                "tool_call_id": "call-1",
                "content": "<tool_error>bash failed</tool_error>",
            }
        ),
        1,
    ).trajectory[0]
    cancelled = observer.parse_record(
        json.dumps(
            {
                "role": "tool",
                "name": "bash",
                "tool_call_id": "call-2",
                "content": "<user_cancellation>stopped</user_cancellation>",
                "tool_result": {"cancelled": True, "duration": 0.01},
            }
        ),
        2,
    ).trajectory[0]

    assert failed.status is TrajectoryStatus.ERROR
    assert failed.failure is not None
    assert failed.failure.detail == "bash failed"
    assert cancelled.status is TrajectoryStatus.CANCELLED
    assert cancelled.timing is not None
    assert cancelled.timing.duration_ms == 10


def test_vibe_injected_retry_keeps_turn_and_regular_user_opens_next() -> None:
    observer = VibeObserver()
    records = [
        {"role": "user", "content": "one", "message_id": "turn-1"},
        {"role": "assistant", "content": "retrying", "message_id": "assistant-1"},
        {"role": "user", "content": "retry", "message_id": "injected-1", "injected": True},
        {"role": "assistant", "content": "done", "message_id": "assistant-2"},
        {"role": "user", "content": "two", "message_id": "turn-2", "injected": False},
    ]
    facts = [
        fact
        for index, record in enumerate(records)
        for fact in observer.parse_record(json.dumps(record), index).trajectory
    ]

    assert [fact.turn_id for fact in facts] == [
        "turn-1",
        "turn-1",
        "turn-1",
        "turn-1",
        "turn-2",
    ]


def test_vibe_history_seed_restores_turn_before_page() -> None:
    observer = VibeObserver()
    prefix = "\n".join(
        json.dumps(record)
        for record in (
            {"role": "user", "content": "inspect", "message_id": "turn-1"},
            {
                "role": "assistant",
                "tool_calls": [{"id": "call-1", "function": {"name": "bash", "arguments": "{}"}}],
            },
        )
    )
    stream = io.BytesIO((prefix + "\n").encode())
    observer._seed_history_context(stream, len(stream.getvalue()))
    result = observer.parse_record(
        json.dumps(
            {
                "role": "tool",
                "name": "bash",
                "tool_call_id": "call-1",
                "content": "done",
            }
        ),
        2,
    )

    assert result.trajectory[0].turn_id == "turn-1"


def test_vibe_history_page_seeds_turn_without_mutating_live_parser(
    tmp_path: Path, workdir: Path
) -> None:
    root = tmp_path / "sessions"
    session = root / "session_20260827_120000_12345678"
    session.mkdir(parents=True)
    records = [
        {"role": "user", "content": "inspect", "message_id": "turn-1"},
        {
            "role": "assistant",
            "tool_calls": [{"id": "call-1", "function": {"name": "bash", "arguments": "{}"}}],
        },
        {"role": "tool", "name": "bash", "tool_call_id": "call-1", "content": "done"},
        {"role": "assistant", "content": "finished", "message_id": "assistant-1"},
    ]
    transcript = session / "messages.jsonl"
    transcript.write_text("".join(f"{json.dumps(record)}\n" for record in records))
    (session / "meta.json").write_text(
        json.dumps(
            {
                "session_id": "12345678-abcd",
                "environment": {"working_directory": str(workdir)},
            }
        )
    )
    observer = VibeObserver(root=root)
    source = observer.open_source(cwd=str(workdir))

    page = asyncio.run(source.history_page(limit=2))

    assert len(page.trajectory) == 2
    assert {fact.turn_id for fact in page.trajectory} == {"turn-1"}
    assert source._observer is not None
    assert source._observer.current_turn_id is None


def test_vibe_live_attachment_seeds_an_in_progress_turn(tmp_path: Path, workdir: Path) -> None:
    root = tmp_path / "sessions"
    session = root / "session_20260827_120000_12345678"
    session.mkdir(parents=True)
    transcript = session / "messages.jsonl"
    prefix = [
        {"role": "user", "content": "inspect", "message_id": "turn-1"},
        {
            "role": "assistant",
            "tool_calls": [{"id": "call-1", "function": {"name": "bash", "arguments": "{}"}}],
        },
    ]
    transcript.write_text("".join(f"{json.dumps(record)}\n" for record in prefix))
    (session / "meta.json").write_text(
        json.dumps(
            {
                "session_id": "12345678-abcd",
                "environment": {"working_directory": str(workdir)},
            }
        )
    )
    source = VibeObserver(root=root).open_source(cwd=str(workdir))
    attached = asyncio.run(source.read())
    assert attached.attached is not None
    source.commit_attachment()
    with transcript.open("a") as fh:
        fh.write(
            json.dumps(
                {
                    "role": "tool",
                    "name": "bash",
                    "tool_call_id": "call-1",
                    "content": "done",
                }
            )
            + "\n"
        )

    batch = asyncio.run(source.read())

    assert len(batch.trajectory) == 1
    assert batch.trajectory[0].turn_id == "turn-1"


def test_vibe_context_boundary_is_a_context_fact() -> None:
    fact = (
        VibeObserver()
        .parse_record(
            json.dumps(
                {
                    "role": "user",
                    "content": "compacted context",
                    "message_id": "context-1",
                    "injected": True,
                    "context_boundary": "compaction",
                }
            ),
            0,
        )
        .trajectory[0]
    )

    assert fact.kind is TrajectoryKind.CONTEXT
    assert fact.turn_id == "context-1"
    assert next(
        detail for detail in fact.details if detail.name == "context_boundary"
    ).preview.text == ("compaction")


def test_vibe_capabilities_match_enriched_transcript_data() -> None:
    capabilities = VibeObserver.trajectory_capabilities

    assert {
        TrajectoryFeature.MODELS,
        TrajectoryFeature.USAGE,
        TrajectoryFeature.TIMING,
        TrajectoryFeature.CONTEXT,
    } <= capabilities.supported
    assert capabilities.unsupported == frozenset(
        {TrajectoryFeature.REQUESTS, TrajectoryFeature.RETRIES}
    )


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


def test_vibe_extracts_theater_mcp_identity_without_classifying_it() -> None:
    observer = VibeObserver()
    call = observer.parse_record(
        json.dumps(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {"name": "theater_send", "arguments": "{}"},
                    }
                ],
            }
        ),
        0,
    ).trajectory[-1]
    result = observer.parse_record(
        json.dumps(
            {
                "role": "tool",
                "name": "theater_send",
                "tool_call_id": "call-1",
                "content": "sent",
            }
        ),
        1,
    ).trajectory[0]

    assert call.kind is TrajectoryKind.TOOL_CALL
    assert result.kind is TrajectoryKind.TOOL_RESULT
    assert (call.mcp_server, call.mcp_tool) == ("theater", "send")
    assert (result.mcp_server, result.mcp_tool) == ("theater", "send")


def test_vibe_canonicalizes_the_wait_only_theater_server() -> None:
    observer = VibeObserver()
    call = observer.parse_record(
        json.dumps(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {"name": "theater_wait_await_sessions", "arguments": "{}"},
                    }
                ],
            }
        ),
        0,
    ).trajectory[-1]
    result = observer.parse_record(
        json.dumps(
            {
                "role": "tool",
                "name": "theater_wait_await_sessions",
                "tool_call_id": "call-1",
                "content": "done",
            }
        ),
        1,
    ).trajectory[0]

    assert (call.mcp_server, call.mcp_tool) == ("theater", "await_sessions")
    assert (result.mcp_server, result.mcp_tool) == ("theater", "await_sessions")


def test_vibe_canonicalizes_the_wait_alias_in_named_mcp_results() -> None:
    assert _vibe_named_mcp_identity("theater_wait_await_sessions", "await_sessions") == (
        "theater",
        "await_sessions",
    )


def test_vibe_extracts_native_mcp_identity_from_presentation_and_result() -> None:
    observer = VibeObserver()
    call = observer.parse_record(
        json.dumps(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {"name": "grafana_query_prometheus", "arguments": "{}"},
                        "presentation": {
                            "kind": "tool",
                            "display": {
                                "message": "grafana_query_prometheus",
                                "statusText": "Calling MCP tool query_prometheus",
                            },
                        },
                    }
                ],
            }
        ),
        0,
    ).trajectory[-1]
    result = observer.parse_record(
        json.dumps(
            {
                "role": "tool",
                "name": "grafana_query_prometheus",
                "tool_call_id": "call-1",
                "content": "ok",
                "tool_result": {
                    "output": {
                        "ok": True,
                        "server": "https://grafana.example/mcp",
                        "tool": "query_prometheus",
                    }
                },
            }
        ),
        1,
    ).trajectory[0]

    assert (call.mcp_server, call.mcp_tool) == ("grafana", "query_prometheus")
    assert (result.mcp_server, result.mcp_tool) == ("grafana", "query_prometheus")


def test_claude_extracts_and_remembers_mcp_identity_for_results() -> None:
    observer = ClaudeCodeObserver()
    call = observer.parse_record(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "message-1",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call-1",
                            "name": "mcp__theater__send",
                            "input": {},
                        }
                    ],
                },
            }
        ),
        0,
    ).trajectory[0]
    result = observer.parse_record(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "sent"}]
                },
            }
        ),
        1,
    ).trajectory[0]

    assert call.kind is TrajectoryKind.TOOL_CALL
    assert result.kind is TrajectoryKind.TOOL_RESULT
    assert (call.mcp_server, call.mcp_tool) == ("theater", "send")
    assert (result.mcp_server, result.mcp_tool) == ("theater", "send")


def test_codex_extracts_and_remembers_mcp_identity_for_results() -> None:
    observer = CodexObserver()
    facts = [
        fact
        for index, line in enumerate(CODEX_FIXTURE.read_text().splitlines())
        for fact in observer.parse_record(line, index).trajectory
        if fact.call_id == "mcp-1"
    ]

    assert [fact.kind for fact in facts] == [
        TrajectoryKind.TOOL_CALL,
        TrajectoryKind.TOOL_RESULT,
    ]
    assert {(fact.mcp_server, fact.mcp_tool) for fact in facts} == {
        ("theater", "list_participants")
    }


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
