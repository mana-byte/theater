"""Focused rich trajectory projections for the Claude and Codex adapters."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import theater.harness.builtin.plugins.claude as claude_plugin
import theater.harness.builtin.plugins.codex as codex_plugin
from theater.harness.builtin.plugins.claude import ClaudeCodeObserver
from theater.harness.builtin.plugins.codex import CodexObserver
from theater.harness.contracts.trajectory import ParsedRecord
from theater.trajectory.enums import TimingProvenance, TrajectoryKind, TrajectoryStatus
from theater.trajectory.records import Timing

FIXTURES = Path(__file__).parent / "fixtures"


def _lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def _facts(observer, lines: list[str]):
    return [
        fact
        for index, line in enumerate(lines)
        for fact in observer.parse_record(line, index).trajectory
    ]


@pytest.mark.parametrize(
    ("module", "observer"),
    [(claude_plugin, ClaudeCodeObserver()), (codex_plugin, CodexObserver())],
)
def test_parse_record_decodes_each_line_once(module, observer, monkeypatch):
    original = module.json.loads
    calls = 0

    def counted(value, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(value, *args, **kwargs)

    monkeypatch.setattr(module.json, "loads", counted)
    parsed = observer.parse_record('{"type":"system","payload":{}}', 0)

    assert isinstance(parsed, ParsedRecord)
    assert calls == 1


@pytest.mark.parametrize(
    ("observer_type", "fixture"),
    [(ClaudeCodeObserver, "trajectory_claude.jsonl"), (CodexObserver, "trajectory_codex.jsonl")],
)
def test_rich_projection_keeps_control_events_equal(observer_type, fixture):
    lines = _lines(fixture)
    control = observer_type()
    rich = observer_type()

    for index, line in enumerate(lines):
        assert list(rich.parse_record(line, index).events) == control.parse(line, index)


@pytest.mark.parametrize("observer_type", [ClaudeCodeObserver, CodexObserver])
def test_malformed_and_non_object_lines_are_empty(observer_type):
    observer = observer_type()
    for index, line in enumerate(("not-json", "null", "[]", "")):
        assert observer.parse_record(line, index) == ParsedRecord()


def test_claude_facts_include_ids_pairing_reasoning_usage_timing_and_system_context():
    facts = _facts(ClaudeCodeObserver(), _lines("trajectory_claude.jsonl"))
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
    assert usage.usage.model == "claude-test"
    assert usage.usage.request_id == "request-1"
    assert usage.timing is not None
    assert usage.timing.provenance is TimingProvenance.SOURCE
    assert duration.timing == Timing(
        start=1787479206.0,
        duration_ms=1250,
        provenance=TimingProvenance.SOURCE,
    )
    assert any(fact.kind is TrajectoryKind.CONTEXT for fact in facts)
    assert not any("opaque" in fact.summary for fact in reasoning)


def test_claude_revisions_are_monotonic_and_missing_ids_use_coordinates():
    lines = _lines("trajectory_claude.jsonl")
    facts = _facts(ClaudeCodeObserver(), lines)
    revisions = [fact.revision for fact in facts if fact.native_id == "revision-1"]
    fallback = next(fact for fact in facts if fact.summary == "fallback")

    assert revisions == sorted(revisions)
    assert len(revisions) == 2
    assert next(fact for fact in facts if fact.native_id == "msg-1").revision == 0
    shifted = ClaudeCodeObserver().parse_record(lines[1], 101).trajectory
    assert next(fact for fact in shifted if fact.native_id == "msg-1").revision == 0
    assert fallback.native_id is None
    assert fallback.revision == 0
    assert fallback.raw_index == 8
    assert fallback.event_ordinal == 0


def test_claude_unmatched_result_remains_visible():
    facts = _facts(ClaudeCodeObserver(), _lines("trajectory_claude.jsonl"))
    unmatched = next(fact for fact in facts if fact.summary == "unmatched result")

    assert unmatched.kind is TrajectoryKind.TOOL_RESULT
    assert unmatched.call_id is None
    assert unmatched.status is TrajectoryStatus.COMPLETED


def test_codex_facts_include_rollout_items_calls_parent_ids_reasoning_usage_and_timing():
    facts = _facts(CodexObserver(), _lines("trajectory_codex.jsonl"))
    call = next(fact for fact in facts if fact.native_id == "item-call-1")
    result = next(fact for fact in facts if fact.native_id == "item-result-1")
    mcp_result = next(
        fact
        for fact in facts
        if fact.call_id == "mcp-1" and fact.kind is TrajectoryKind.TOOL_RESULT
    )
    reasoning = next(fact for fact in facts if fact.kind is TrajectoryKind.REASONING)
    usage = next(fact for fact in facts if fact.usage is not None)
    complete = next(
        fact
        for fact in facts
        if fact.native_id == "turn-1:completed"
        and fact.kind is TrajectoryKind.CONTEXT
        and fact.summary == "turn completed"
    )

    assert call.kind is TrajectoryKind.TOOL_CALL
    assert call.call_id == "call-1"
    assert call.parent_call_id == "parent-1"
    assert call.details[0].name == "input"
    assert result.kind is TrajectoryKind.TOOL_RESULT
    assert result.call_id == "call-1"
    assert result.parent_call_id == "call-1"
    assert mcp_result.kind is TrajectoryKind.TOOL_RESULT
    assert mcp_result.parent_call_id == "mcp-1"
    assert mcp_result.status is TrajectoryStatus.COMPLETED
    assert mcp_result.timing is not None
    assert mcp_result.timing.duration_ms == pytest.approx(1500)
    assert reasoning.summary == "explicit summary"
    assert usage.usage is not None
    assert usage.usage.model == "codex-test"
    assert usage.usage.input_tokens == 75
    assert usage.usage.cache_read_tokens == 20
    assert usage.usage.cache_write_tokens == 5
    assert complete.status is TrajectoryStatus.COMPLETED
    assert complete.summary == "turn completed"
    assert complete.timing is not None
    assert complete.timing.duration_ms == 9000


def test_codex_never_projects_encrypted_only_reasoning_and_keeps_unmatched_results():
    facts = _facts(CodexObserver(), _lines("trajectory_codex.jsonl"))
    unmatched = next(fact for fact in facts if fact.call_id == "unmatched-call")

    assert [fact.summary for fact in facts if fact.kind is TrajectoryKind.REASONING] == [
        "explicit summary"
    ]
    assert unmatched.kind is TrajectoryKind.TOOL_RESULT
    assert unmatched.native_id is None
    assert unmatched.parent_call_id == "unmatched-call"
    assert unmatched.status is TrajectoryStatus.COMPLETED


def test_codex_missing_message_id_uses_fallback_coordinates():
    lines = _lines("trajectory_codex.jsonl")
    facts = _facts(CodexObserver(), lines)
    fallback = next(fact for fact in facts if fact.summary == "fallback")

    assert fallback.native_id is None
    assert fallback.raw_index == 12
    assert fallback.event_ordinal == 0


def test_native_revision_is_independent_of_history_parse_order():
    lines = _lines("trajectory_codex.jsonl")
    forward = {
        index: CodexObserver().parse_record(line, index).trajectory
        for index, line in enumerate(lines)
        if "item-call-1" in line
    }
    reverse = {
        index: CodexObserver().parse_record(line, index).trajectory
        for index, line in reversed(list(enumerate(lines)))
        if "item-call-1" in line
    }

    assert forward == reverse
    shifted = CodexObserver().parse_record(lines[4], 404).trajectory
    assert shifted[0].native_id == "item-call-1"
    assert shifted[0].revision == 0


def test_codex_dual_assistant_representations_have_one_canonical_fact():
    lines = _lines("trajectory_codex_dual.jsonl")
    control = CodexObserver()
    rich = CodexObserver()

    facts = [
        fact
        for index, line in enumerate(lines)
        for fact in rich.parse_record(line, index).trajectory
    ]
    assistants = [fact for fact in facts if fact.kind is TrajectoryKind.ASSISTANT]

    assert len(assistants) == 1
    assert assistants[0].native_id == "dual-message-1"
    assert assistants[0].summary == "canonical answer"
    assert [list(rich.parse_record(line, index).events) for index, line in enumerate(lines)] == [
        control.parse(line, index) for index, line in enumerate(lines)
    ]
    event_record = rich.parse_record(lines[0], 0)
    assert event_record.events
    assert event_record.baseline_events == ()


def test_detail_fields_are_bounded_for_large_tool_input():
    line = json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-08-23T12:00:00Z",
            "message": {
                "id": "large-message",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "large-call",
                        "name": "exec",
                        "input": {"command": "x" * (20 * 1024)},
                    }
                ],
            },
        }
    )
    fact = ClaudeCodeObserver().parse_record(line, 0).trajectory[0]
    preview = fact.details[0].preview

    assert preview.omitted_bytes > 0
    assert preview.encoded_bytes <= 16 * 1024
    assert fact.summary == "exec"


@pytest.mark.parametrize(
    ("observer", "record"),
    [
        (ClaudeCodeObserver(), {"type": "system", "durationMs": 10**400}),
        (
            CodexObserver(),
            {"type": "turn_context", "payload": {"duration_ms": 10**400}},
        ),
    ],
)
def test_trajectory_numeric_overflow_does_not_break_parsing(observer, record):
    parsed = observer.parse_record(json.dumps(record), 0)

    assert isinstance(parsed, ParsedRecord)
