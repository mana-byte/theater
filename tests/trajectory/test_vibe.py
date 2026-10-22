"""Vibe transcripts projected into trajectory facts: tool results, retries, history, MCP."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

from tests.support.paths import fixture
from tests.support.shipped import VibeObserver
from theater.harness import EventKind
from theater.harness.builtin.plugins.vibe.trajectory import _vibe_named_mcp_identity
from theater.trajectory import ContentFormat, TimingProvenance, TrajectoryKind, TrajectoryStatus
from theater.trajectory.capabilities import TrajectoryFeature

FIXTURE = fixture("vibe_messages.jsonl")


def test_vibe_parse_record_preserves_control_events_and_source_timestamps() -> None:
    observer = VibeObserver()
    for index, line in enumerate(FIXTURE.read_text().splitlines()):
        parsed = observer.parse_record(line, index)
        assert list(parsed.events) == observer.parse(line, index)
        assert all(event.ts is None for event in parsed.events)


def test_vibe_facts_keep_ids_reasoning_and_tool_pairing() -> None:
    observer = VibeObserver()
    records = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    assistant_facts = observer.parse_record(json.dumps(records[2]), 2).facts
    facts = [
        fact
        for index, record in enumerate(records)
        for fact in observer.parse_record(json.dumps(record), index).facts
    ]

    assert [fact.kind for fact in assistant_facts] == [
        TrajectoryKind.REASONING,
        TrajectoryKind.ASSISTANT,
        TrajectoryKind.TOOL_CALL,
    ]
    assert [fact.event_ordinal for fact in assistant_facts] == [0, 1, 2]
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
    tmp_path: Path, workdir: Path, monkeypatch
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

    def unreadable_seed(self, fh, start):
        raise OSError("seed read failed")

    monkeypatch.setattr(VibeObserver, "_seed_history_context", unreadable_seed)
    failed = asyncio.run(source.history_page(limit=2))
    assert failed.error_code == "transcript_source_unavailable"


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
