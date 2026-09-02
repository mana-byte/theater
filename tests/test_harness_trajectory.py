"""Focused rich trajectory projections for the Claude and Codex adapters."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import theater.harness.builtin.plugins.claude.parser as claude_parser
import theater.harness.builtin.plugins.claude.usage as claude_plugin
import theater.harness.builtin.plugins.opencode as opencode_plugin
from theater.daemon.trajectory.project import fact_to_record
from theater.harness.builtin.plugins.claude.observer import ClaudeCodeObserver
from theater.harness.builtin.plugins.codex import parser as codex_plugin
from theater.harness.builtin.plugins.codex.observer import CodexObserver
from theater.harness.builtin.plugins.codex.values import _codex_usage
from theater.harness.contracts.trajectory import ParsedRecord
from theater.pricing import usage_cost_microcents
from theater.provenance import TranscriptProvenance
from theater.trajectory.enums import (
    CostProvenance,
    TimingProvenance,
    TrajectoryFailureCategory,
    TrajectoryKind,
    TrajectoryStatus,
)
from theater.trajectory.records import Timing
from theater.trajectory.requests import requests_for_records
from theater.trajectory.tools import tool_operations_for_records

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
    [(claude_parser, ClaudeCodeObserver()), (codex_plugin, CodexObserver())],
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


def test_claude_revisions_are_monotonic_and_missing_ids_use_coordinates():
    lines = _lines("trajectory_claude.jsonl")
    facts = _facts(ClaudeCodeObserver(), lines)
    revisions = [fact.revision for fact in facts if fact.native_id == "revision-1"]
    fallback = next(fact for fact in facts if fact.summary == "fallback")

    assert revisions == sorted(revisions)
    assert len(revisions) == 2
    assert next(fact for fact in facts if fact.native_id == "assistant-1").revision == 0
    shifted = ClaudeCodeObserver().parse_record(lines[1], 101).trajectory
    assert next(fact for fact in shifted if fact.native_id == "assistant-1").revision == 0
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
    facts = _facts(ClaudeCodeObserver(), [json.dumps(record) for record in records])
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


async def test_claude_history_seeds_parent_timing_before_the_selected_page(tmp_path: Path):
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

    page = await source.history_page(limit=1)

    assert len(page.trajectory) == 1
    assert page.trajectory[0].timing == Timing(
        start=1787479200.0,
        end=1787479202.0,
        duration_ms=2_000,
        provenance=TimingProvenance.DERIVED,
        first_token=1787479202.0,
    )


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
    facts = _facts(ClaudeCodeObserver(), [json.dumps(record) for record in records])
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
    assert usage.kind is TrajectoryKind.USAGE
    assert usage.summary == ""
    assert usage.usage is not None
    assert usage.usage.model == "codex-test"
    assert usage.usage.provider == "openai"
    assert usage.usage.input_tokens == 75
    assert usage.usage.cache_read_tokens == 20
    assert usage.usage.cache_write_tokens == 5
    assert complete.status is TrajectoryStatus.COMPLETED
    assert complete.summary == "turn completed"
    assert complete.timing is not None
    assert complete.timing.duration_ms == 9000


def test_codex_thread_settings_seed_usage_without_adding_trajectory_noise() -> None:
    observer = CodexObserver()
    settings = observer.parse_record(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "thread_settings_applied",
                    "thread_settings": {
                        "model": "gpt-5.6-sol",
                        "model_provider_id": "azure",
                    },
                },
            }
        ),
        0,
    )

    assert settings.trajectory == ()

    usage = observer.parse_record(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 20,
                            "output_tokens": 30,
                        }
                    },
                },
            }
        ),
        1,
    ).trajectory[0]
    context = observer.parse_record(
        json.dumps(
            {
                "type": "turn_context",
                "payload": {"turn_id": "turn-1", "model": "gpt-5.6-sol"},
            }
        ),
        2,
    ).trajectory[0]

    assert usage.usage is not None
    assert usage.usage.model == "gpt-5.6-sol"
    assert usage.usage.provider == "azure"
    assert context.kind is TrajectoryKind.CONTEXT
    assert context.summary == "turn context: gpt-5.6-sol"


async def test_codex_history_associates_context_model_and_usage_with_the_active_turn(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rollout.jsonl"
    records = [
        {
            "type": "session_meta",
            "payload": {"cwd": str(tmp_path), "model_provider": "azure"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-1"},
        },
        {
            "type": "turn_context",
            "payload": {"turn_id": "turn-1", "model": "gpt-5.6-sol"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "answer-1",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}],
                "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 20,
                        "output_tokens": 30,
                        "reasoning_output_tokens": 4,
                    },
                    "total_token_usage": {"input_tokens": 100, "output_tokens": 30},
                },
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "turn-1"},
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    observer = CodexObserver(root=tmp_path)
    source = observer.open_source(
        cwd=str(tmp_path),
        known_location=str(path),
        session_provenance=TranscriptProvenance.OPERATOR,
    )

    page = await source.history_page(limit=20)
    usage_fact = next(fact for fact in page.trajectory if fact.usage is not None)
    canonical = tuple(
        fact_to_record(fact, participant_id="participant", source_epoch="epoch")
        for fact in page.trajectory
    )
    request = next(
        request
        for request in requests_for_records(canonical)
        if request.source_request_id == "turn-1"
    )

    assert usage_fact.turn_id == "turn-1"
    assert usage_fact.request_id == "turn-1"
    assert usage_fact.usage is not None
    assert usage_fact.usage.model == "gpt-5.6-sol"
    assert usage_fact.usage.provider == "azure"
    assert request.model == "gpt-5.6-sol"
    assert request.provider == "azure"
    assert request.usage is not None
    assert request.usage.input_tokens == 80


async def test_codex_live_attachment_seeds_model_context_for_usage(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    records = [
        {
            "type": "session_meta",
            "payload": {"cwd": str(tmp_path), "model_provider": "openai"},
        },
        {
            "type": "turn_context",
            "payload": {"turn_id": "turn-1", "model": "gpt-5.6-sol"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "work"},
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    observer = CodexObserver(root=tmp_path)
    source = observer.open_source(
        cwd=str(tmp_path),
        known_location=str(path),
        session_provenance=TranscriptProvenance.OPERATOR,
    )

    attached = await source.read()
    assert attached.attached is not None
    source.commit_attachment()

    usage_record = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 20,
                    "output_tokens": 30,
                    "reasoning_output_tokens": 4,
                },
                "total_token_usage": {"input_tokens": 100, "output_tokens": 30},
            },
        },
    }
    with path.open("a") as transcript:
        transcript.write(json.dumps(usage_record) + "\n")

    batch = await source.read()
    usage = next(event.usage for event in batch.events if event.usage is not None)

    assert usage.model == "gpt-5.6-sol"
    assert usage.provider == "openai"
    assert usage_cost_microcents(usage) > 0


def test_codex_task_complete_preserves_explicit_first_token_time() -> None:
    line = json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn",
                "started_at": 100.0,
                "completed_at": 102.0,
                "time_to_first_token_ms": 250.0,
            },
        }
    )
    fact = CodexObserver().parse_record(line, 0).trajectory[0]

    assert fact.timing == Timing(
        start=100.0,
        end=102.0,
        first_token=100.25,
        provenance=TimingProvenance.SOURCE,
    )

    without_first_token = json.dumps(
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "started_at": 100.0, "completed_at": 102.0},
        }
    )
    missing = CodexObserver().parse_record(without_first_token, 0).trajectory[0]
    assert missing.timing is not None and missing.timing.first_token is None


def test_cost_provenance_tracks_reported_adapter_values() -> None:
    claude = claude_plugin._token_usage({"usage": {"input_tokens": 1}}, {"costUSD": 0.1})
    codex = _codex_usage({}, {"usage": {"input_tokens": 1, "cost_usd": 0.2}})
    opencode = opencode_plugin._opencode_usage({"tokens": {"input": 1}, "cost": 0.3})

    assert claude is not None and claude.cost_provenance is CostProvenance.REPORTED
    assert codex is not None and codex.cost_provenance is CostProvenance.REPORTED
    assert opencode is not None and opencode.cost_provenance is CostProvenance.REPORTED


def test_provider_projection_stays_inside_each_adapter() -> None:
    claude = claude_plugin._claude_trajectory_usage(
        {"provider": "anthropic", "usage": {"input_tokens": 1}},
        {},
    )
    opencode = opencode_plugin._trajectory_usage(
        {
            "providerID": "openai-foundry",
            "modelID": "zai-glm-5-2",
            "tokens": {"input": 1},
        }
    )

    assert claude is not None and claude.provider == "anthropic"
    assert opencode is not None and opencode.provider == "openai-foundry"


def test_explicit_tool_and_provider_errors_keep_typed_failures() -> None:
    tool_line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": "message",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool",
                        "is_error": True,
                        "content": "bad",
                    }
                ],
            },
        }
    )
    provider_line = json.dumps({"type": "system", "level": "error", "error": "API failed"})
    observer = ClaudeCodeObserver()
    tool = observer.parse_record(tool_line, 0).trajectory[0]
    provider = observer.parse_record(provider_line, 1).trajectory[0]

    assert tool.failure is not None and tool.failure.category is TrajectoryFailureCategory.TOOL
    assert provider.failure is not None
    assert provider.failure.category is TrajectoryFailureCategory.PROVIDER

    status_only = json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool",
                        "status": "error",
                        "content": "no explicit error",
                    }
                ]
            },
        }
    )
    unclassified = observer.parse_record(status_only, 2).trajectory[0]
    assert unclassified.status is TrajectoryStatus.ERROR
    assert unclassified.failure is None


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
