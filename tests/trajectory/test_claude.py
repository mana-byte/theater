"""Claude transcript records projected into trajectory facts: ids, split blocks, timing, history."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support.trajectory_parsing import fixture_lines, parse_facts
from theater.daemon.trajectory.project import fact_to_record
from theater.harness.builtin.plugins.claude.observer import ClaudeCodeObserver
from theater.provenance import TranscriptProvenance
from theater.trajectory.enums import (
    TimingProvenance,
    TrajectoryKind,
    TrajectoryStatus,
)
from theater.trajectory.records import Timing
from theater.trajectory.requests import requests_for_records
from theater.trajectory.tools import tool_operations_for_records


def test_claude_facts_include_ids_pairing_reasoning_usage_timing_and_system_context():
    facts = parse_facts(ClaudeCodeObserver(), fixture_lines("trajectory_claude.jsonl"))
    reasoning = [fact for fact in facts if fact.kind is TrajectoryKind.REASONING]
    call = next(fact for fact in facts if fact.kind is TrajectoryKind.TOOL_CALL)
    result = next(fact for fact in facts if fact.kind is TrajectoryKind.TOOL_RESULT)
    usage = next(fact for fact in facts if fact.usage is not None)
    duration = next(fact for fact in facts if fact.native_id == "timing-1")

    assert any(fact.summary == "explicit thought" for fact in reasoning)
    assert call.native_id == "tool-1"
    assert call.call_id == "tool-1"
    assert call.details[0].name == "input"
    assert result.call_id == "tool-1"
    assert result.parent_call_id == "tool-1"
    assert result.status is TrajectoryStatus.COMPLETED
    assert all(
        fact.status is TrajectoryStatus.COMPLETED
        for fact in facts
        if fact.kind in (TrajectoryKind.USER, TrajectoryKind.ASSISTANT, TrajectoryKind.REASONING)
    )
    assert usage.usage is not None
    assert usage.kind is TrajectoryKind.USAGE
    assert usage.usage.model == "claude-test"
    assert usage.usage.request_id == "request-1"
    assert usage.timing is not None
    assert usage.timing.provenance is TimingProvenance.SOURCE
    assert duration.timing == Timing(
        start=1787479204.75,
        end=1787479206.0,
        duration_ms=1250,
        provenance=TimingProvenance.DERIVED,
    )
    assert any(fact.kind is TrajectoryKind.CONTEXT for fact in facts)
    assert not any("opaque" in fact.summary for fact in reasoning)


def test_claude_facts_without_native_ids_use_coordinates():
    lines = fixture_lines("trajectory_claude.jsonl")
    facts = parse_facts(ClaudeCodeObserver(), lines)
    fallback = next(fact for fact in facts if fact.summary == "fallback")

    assert fallback.native_id is None
    assert fallback.revision == 0
    assert fallback.raw_index == 8
    assert fallback.event_ordinal == 0


def test_claude_split_blocks_keep_unique_ids_and_derive_request_timing():
    records = [
        {
            "type": "user",
            "uuid": "input-1",
            "promptId": "turn-1",
            "timestamp": "2026-08-23T10:00:00.000Z",
            "message": {"content": "inspect it"},
        },
        {
            "type": "assistant",
            "uuid": "thinking-1",
            "parentUuid": "input-1",
            "requestId": "request-1",
            "timestamp": "2026-08-23T10:00:01.000Z",
            "message": {
                "id": "message-1",
                "content": [{"type": "thinking", "thinking": "checking"}],
            },
        },
        {
            "type": "assistant",
            "uuid": "answer-1",
            "parentUuid": "thinking-1",
            "requestId": "request-1",
            "timestamp": "2026-08-23T10:00:02.000Z",
            "message": {
                "id": "message-1",
                "content": [{"type": "text", "text": "done"}],
                "stop_reason": "end_turn",
            },
        },
        {
            "type": "system",
            "uuid": "duration-1",
            "parentUuid": "answer-1",
            "subtype": "turn_duration",
            "durationMs": 3_000,
            "timestamp": "2026-08-23T10:00:03.000Z",
        },
    ]
    facts = parse_facts(ClaudeCodeObserver(), [json.dumps(record) for record in records])
    reasoning = next(fact for fact in facts if fact.kind is TrajectoryKind.REASONING)
    answer = next(fact for fact in facts if fact.kind is TrajectoryKind.ASSISTANT)
    duration = next(fact for fact in facts if fact.native_id == "duration-1")

    assert reasoning.native_id == "thinking-1"
    assert answer.native_id == "answer-1"
    assert reasoning.request_id == answer.request_id == "request-1"
    assert reasoning.turn_id == answer.turn_id == "turn-1"
    assert reasoning.timing == Timing(
        start=1787479200.0,
        end=1787479201.0,
        duration_ms=1_000,
        provenance=TimingProvenance.DERIVED,
        first_token=1787479201.0,
    )
    assert answer.timing == Timing(
        start=1787479200.0,
        end=1787479202.0,
        duration_ms=2_000,
        provenance=TimingProvenance.DERIVED,
        first_token=1787479201.0,
    )
    assert duration.turn_id == "turn-1"
    assert duration.request_id is None
    assert duration.timing == Timing(
        start=1787479200.0,
        end=1787479203.0,
        duration_ms=3_000,
        provenance=TimingProvenance.DERIVED,
    )

    canonical = tuple(
        fact_to_record(fact, participant_id="participant", source_epoch="epoch") for fact in facts
    )
    request = next(
        request
        for request in requests_for_records(canonical)
        if request.source_request_id == "request-1"
    )
    assert request.timing == Timing(
        start=1787479200.0,
        end=1787479202.0,
        duration_ms=2_000,
        provenance=TimingProvenance.DERIVED,
        first_token=1787479201.0,
    )


async def test_claude_history_seeds_parent_timing_before_the_selected_page(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "projects"
    path = root / "-repo" / "session.jsonl"
    path.parent.mkdir(parents=True)
    records = [
        {
            "type": "user",
            "uuid": "input-1",
            "timestamp": "2026-08-23T10:00:00.000Z",
            "message": {"content": "inspect it"},
        },
        {
            "type": "assistant",
            "uuid": "answer-1",
            "parentUuid": "input-1",
            "requestId": "request-1",
            "timestamp": "2026-08-23T10:00:02.000Z",
            "message": {
                "id": "message-1",
                "content": [{"type": "text", "text": "done"}],
                "stop_reason": "end_turn",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    source = ClaudeCodeObserver(root=root).open_source_for(
        participant_id="participant",
        cwd=str(tmp_path),
        known_location=str(path),
        session_provenance=TranscriptProvenance.OPERATOR,
    )
    source._observer._main_turn_id = "live-turn"

    page = await source.history_page(limit=1)

    assert source._observer._main_turn_id == "live-turn"
    assert len(page.trajectory) == 1
    assert page.trajectory[0].timing == Timing(
        start=1787479200.0,
        end=1787479202.0,
        duration_ms=2_000,
        provenance=TimingProvenance.DERIVED,
        first_token=1787479202.0,
    )

    def unreadable_seed(self, fh, start):
        raise OSError("seed read failed")

    monkeypatch.setattr(ClaudeCodeObserver, "_seed_mcp_context", unreadable_seed)
    failed = await source.history_page(limit=1)
    assert failed.error_code == "transcript_source_unavailable"
    assert source._observer._main_turn_id == "live-turn"


def test_claude_request_inference_does_not_change_tool_execution_timing():
    records = [
        {
            "type": "user",
            "uuid": "input-1",
            "timestamp": "2026-08-23T10:00:00.000Z",
            "message": {"content": "inspect it"},
        },
        {
            "type": "assistant",
            "uuid": "call-record-1",
            "parentUuid": "input-1",
            "requestId": "request-1",
            "timestamp": "2026-08-23T10:00:02.000Z",
            "message": {
                "id": "message-1",
                "model": "claude-test",
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "content": [{"type": "tool_use", "id": "call-1", "name": "Read"}],
            },
        },
        {
            "type": "user",
            "uuid": "result-record-1",
            "parentUuid": "call-record-1",
            "timestamp": "2026-08-23T10:00:05.000Z",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "ok"}]
            },
        },
    ]
    facts = parse_facts(ClaudeCodeObserver(), [json.dumps(record) for record in records])
    canonical = tuple(
        fact_to_record(fact, participant_id="participant", source_epoch="epoch") for fact in facts
    )
    operation = tool_operations_for_records(canonical)[0]
    request = next(
        request
        for request in requests_for_records(canonical)
        if request.source_request_id == "request-1"
    )

    assert operation.timing == Timing(
        start=1787479202.0,
        end=1787479205.0,
        duration_ms=3_000,
        provenance=TimingProvenance.DERIVED,
    )
    assert request.timing == Timing(
        start=1787479200.0,
        end=1787479202.0,
        duration_ms=2_000,
        provenance=TimingProvenance.DERIVED,
        first_token=1787479202.0,
    )


def test_claude_unmatched_result_remains_visible():
    facts = parse_facts(ClaudeCodeObserver(), fixture_lines("trajectory_claude.jsonl"))
    unmatched = next(fact for fact in facts if fact.summary == "unmatched result")

    assert unmatched.kind is TrajectoryKind.TOOL_RESULT
    assert unmatched.call_id is None
    assert unmatched.status is TrajectoryStatus.COMPLETED


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
