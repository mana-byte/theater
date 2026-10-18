"""Pi parser tool projection, assistant timing and reasoning."""

from __future__ import annotations

import json

import pytest

from tests.support.pi_transcripts import message_record
from theater.daemon.trajectory.project import fact_to_record
from theater.harness.builtin.plugins.pi.observer import PiObserver
from theater.trajectory.enums import (
    TimingProvenance,
    TrajectoryKind,
    TrajectoryLane,
)


@pytest.mark.parametrize(
    ("name", "identity"),
    [
        ("grafana__query_prometheus", ("grafana", "query_prometheus")),
        ("bash", (None, None)),
        ("__missing_server", (None, None)),
        ("missing_tool__", (None, None)),
    ],
)
def test_pi_parser_identifies_mcp_tool_calls_and_results(tmp_path, name, identity) -> None:
    observer = PiObserver(root=tmp_path)
    call = observer.parse_record(
        json.dumps(
            message_record(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "call-1",
                            "name": name,
                            "arguments": {},
                        }
                    ],
                    "stopReason": "toolUse",
                },
            )
        ),
        1,
    )
    result = observer.parse_record(
        json.dumps(
            message_record(
                "result-1",
                {
                    "role": "toolResult",
                    "toolCallId": "call-1",
                    "toolName": name,
                    "content": [{"type": "text", "text": "ok"}],
                },
            )
        ),
        2,
    )

    assert (call.trajectory[-1].mcp_server, call.trajectory[-1].mcp_tool) == identity
    assert (result.trajectory[0].mcp_server, result.trajectory[0].mcp_tool) == identity


def test_pi_parser_derives_assistant_timing_from_inner_start_and_outer_completion(tmp_path) -> None:
    # Pi writes two timestamps: inner message.timestamp (set by pi-ai when the
    # pending response object is created, ~= generation start) and outer
    # record.timestamp (set by the session manager on message_end, ~= completion).
    # The parser must pair them into a DERIVED interval instead of mislabeling the
    # inner timestamp as the end and discarding the outer one.
    observer = PiObserver(root=tmp_path)
    inner_epoch_ms = 1788254909379
    record = {
        "type": "message",
        "id": "assistant-1",
        "timestamp": "2026-09-01T09:28:39.296Z",  # outer (completion)
        "message": {
            "role": "assistant",
            "timestamp": inner_epoch_ms,  # inner (start)
            "content": [{"type": "text", "text": "hi"}],
            "stopReason": "stop",
        },
    }
    parsed = observer.parse_record(json.dumps(record), 1)
    assistant = next(f for f in parsed.trajectory if f.kind is TrajectoryKind.ASSISTANT)
    assert assistant.timing is not None
    assert assistant.timing.provenance is TimingProvenance.DERIVED
    assert assistant.timing.start == inner_epoch_ms / 1_000
    timing = assistant.timing
    assert timing.end is not None
    assert timing.start is not None
    assert timing.duration_ms is not None
    assert timing.duration_ms == pytest.approx((timing.end - timing.start) * 1_000)
    # Directly assert the end is the outer (completion) timestamp, not just self-consistent.
    assert timing.end == 1788254919.296  # 2026-09-01T09:28:39.296Z
    # Control Event.ts must prefer the outer (completion) timestamp, not the
    # inner generation-start, so the bus stamp is the moment Pi finalized.
    assert parsed.events
    assert parsed.events[0].ts == 1788254919.296


def test_pi_parser_anchors_tool_call_start_at_containing_assistant_completion(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    record = {
        "type": "message",
        "id": "assistant-1",
        "timestamp": "2026-09-01T09:28:39.296Z",  # outer (completion)
        "message": {
            "role": "assistant",
            "timestamp": 1788254909379,  # inner (start)
            "content": [
                {
                    "type": "toolCall",
                    "id": "call-1",
                    "name": "bash",
                    "arguments": {"command": "echo hi"},
                }
            ],
            "stopReason": "toolUse",
        },
    }
    parsed = observer.parse_record(json.dumps(record), 1)
    tool_call = next(f for f in parsed.trajectory if f.kind is TrajectoryKind.TOOL_CALL)
    assert tool_call.timing is not None
    assert tool_call.timing.provenance is TimingProvenance.DERIVED
    # Tool-call start is the assistant's OUTER (completion) timestamp, not the inner.
    assert tool_call.timing.start == 1788254919.296
    assert tool_call.timing.end is None


def test_pi_parser_falls_back_to_start_source_when_only_inner_timestamp_exists(tmp_path) -> None:
    # When the outer record.timestamp is absent, the inner timestamp is treated as
    # the start (not the end) so the original mislabeling bug never returns.
    observer = PiObserver(root=tmp_path)
    inner_epoch_ms = 1788254909379
    record = {
        "type": "message",
        "id": "assistant-1",
        "message": {
            "role": "assistant",
            "timestamp": inner_epoch_ms,
            "content": [{"type": "text", "text": "hi"}],
            "stopReason": "stop",
        },
    }
    parsed = observer.parse_record(json.dumps(record), 1)
    assistant = next(f for f in parsed.trajectory if f.kind is TrajectoryKind.ASSISTANT)
    assert assistant.timing is not None
    assert assistant.timing.provenance is TimingProvenance.SOURCE
    assert assistant.timing.start == inner_epoch_ms / 1_000
    assert assistant.timing.end is None


def test_pi_parser_inner_only_assistant_usage_uses_start_not_end(tmp_path) -> None:
    # When only the inner timestamp exists, the assistant usage fact must agree with
    # the assistant/reasoning facts: start-only SOURCE. It must not fall back to the
    # old end-only _timing() path.
    observer = PiObserver(root=tmp_path)
    inner_epoch_ms = 1788254909379
    record = {
        "type": "message",
        "id": "assistant-1",
        "message": {
            "role": "assistant",
            "timestamp": inner_epoch_ms,
            "content": [{"type": "text", "text": "hi"}],
            "stopReason": "stop",
            "usage": {
                "input": 10,
                "output": 5,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": 15,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
            },
        },
    }
    parsed = observer.parse_record(json.dumps(record), 1)
    assistant = next(f for f in parsed.trajectory if f.kind is TrajectoryKind.ASSISTANT)
    usage = next(f for f in parsed.trajectory if f.kind is TrajectoryKind.USAGE)
    assert assistant.timing is not None
    assert assistant.timing.provenance is TimingProvenance.SOURCE
    assert assistant.timing.start == inner_epoch_ms / 1_000
    assert assistant.timing.end is None
    # Usage fact must agree with the assistant fact: start-only SOURCE, not end-only.
    assert usage.timing is not None
    assert usage.timing.provenance is TimingProvenance.SOURCE
    assert usage.timing.start == inner_epoch_ms / 1_000
    assert usage.timing.end is None


def test_pi_parser_falls_back_to_end_source_when_only_outer_timestamp_exists(tmp_path) -> None:
    # When the inner message.timestamp is absent but the outer record.timestamp is
    # present, the assistant record becomes Timing(end=outer, SOURCE) -- NOT a
    # zero-width Timing(start=outer, end=outer, DERIVED) interval.
    observer = PiObserver(root=tmp_path)
    record = {
        "type": "message",
        "id": "assistant-1",
        "timestamp": "2026-09-01T09:28:39.296Z",  # outer only
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
            "stopReason": "stop",
        },
    }
    parsed = observer.parse_record(json.dumps(record), 1)
    assistant = next(f for f in parsed.trajectory if f.kind is TrajectoryKind.ASSISTANT)
    assert assistant.timing is not None
    assert assistant.timing.provenance is TimingProvenance.SOURCE
    assert assistant.timing.start is None
    assert assistant.timing.end == 1788254919.296  # 2026-09-01T09:28:39.296Z
    assert assistant.timing.duration_ms is None


def test_pi_parser_drops_interval_when_outer_precedes_inner(tmp_path) -> None:
    # If the outer (completion) timestamp precedes the inner (start) -- a malformed
    # or clock-skewed record -- the interval is not built as a negative-duration
    # DERIVED; the inner is kept as a start-only SOURCE point.
    observer = PiObserver(root=tmp_path)
    record = {
        "type": "message",
        "id": "assistant-1",
        "timestamp": "2026-09-01T09:28:29.000Z",  # outer EARLIER than inner
        "message": {
            "role": "assistant",
            "timestamp": 1788254919379,  # inner later (09:28:39.379Z)
            "content": [{"type": "text", "text": "hi"}],
            "stopReason": "stop",
        },
    }
    parsed = observer.parse_record(json.dumps(record), 1)
    assistant = next(f for f in parsed.trajectory if f.kind is TrajectoryKind.ASSISTANT)
    assert assistant.timing is not None
    # outer < inner => no DERIVED interval; inner retained as start-only SOURCE.
    assert assistant.timing.provenance is TimingProvenance.SOURCE
    assert assistant.timing.start == 1788254919.379
    assert assistant.timing.end is None
    assert assistant.timing.duration_ms is None


def test_pi_parser_extracts_reasoning_from_thinking_blocks(tmp_path) -> None:
    # Pi ThinkingContent is {"type":"thinking","thinking":str,...}; the parser
    # previously read only "text" (a TextContent field), so REASONING facts were
    # always empty.
    observer = PiObserver(root=tmp_path)
    parsed = observer.parse_record(
        json.dumps(
            message_record(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "answer"},
                        {"type": "thinking", "thinking": "I should inspect the tree first."},
                        {"type": "thinking"},  # redacted / empty
                        {"type": "thinking", "thinking": ""},  # explicit empty
                    ],
                    "stopReason": "stop",
                },
            )
        ),
        1,
    )
    reasoning = [fact for fact in parsed.trajectory if fact.kind is TrajectoryKind.REASONING]
    assert [fact.summary for fact in reasoning] == ["I should inspect the tree first."]
    assistant = next(fact for fact in parsed.trajectory if fact.kind is TrajectoryKind.ASSISTANT)
    assert assistant.summary == "answer"


def test_pi_theater_tools_project_to_theater_activity(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    call = observer.parse_record(
        json.dumps(
            message_record(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "call-1",
                            "name": "theater__whoami",
                            "arguments": {},
                        }
                    ],
                    "stopReason": "toolUse",
                },
            )
        ),
        1,
    ).trajectory[-1]
    result = observer.parse_record(
        json.dumps(
            message_record(
                "result-1",
                {
                    "role": "toolResult",
                    "toolCallId": "call-1",
                    "toolName": "theater__whoami",
                    "content": [{"type": "text", "text": "ok"}],
                },
            )
        ),
        2,
    ).trajectory[0]

    call_record = fact_to_record(call, participant_id="pi-child", source_epoch="epoch")
    result_record = fact_to_record(result, participant_id="pi-child", source_epoch="epoch")
    assert (call_record.kind, call_record.lane, call_record.summary) == (
        TrajectoryKind.THEATER_CALL,
        TrajectoryLane.THEATER,
        "whoami",
    )
    assert (result_record.kind, result_record.lane, result_record.summary) == (
        TrajectoryKind.THEATER_RESULT,
        TrajectoryLane.THEATER,
        "whoami completed",
    )
