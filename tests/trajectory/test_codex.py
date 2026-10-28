"""Codex rollouts projected into trajectory facts: calls, reasoning, usage, history, seeding."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.trajectory_parsing import fixture_lines, parse_facts
from theater.daemon.trajectory.project import fact_to_record
from theater.harness.builtin.plugins.codex.observer import CodexObserver
from theater.pricing import usage_cost_microcents
from theater.provenance import TranscriptProvenance
from theater.trajectory.enums import (
    ContentFormat,
    TimingProvenance,
    TrajectoryKind,
    TrajectoryStatus,
)
from theater.trajectory.records import Timing
from theater.trajectory.requests import requests_for_records


def test_codex_facts_include_rollout_items_calls_parent_ids_reasoning_usage_and_timing():
    facts = parse_facts(CodexObserver(), fixture_lines("trajectory_codex.jsonl"))
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


def test_codex_call_inputs_are_json_only_when_structured() -> None:
    """Code mode's `exec` input is JavaScript: labelling it JSON left it unparsed plain text."""
    calls = [
        {"type": "custom_tool_call", "call_id": "js", "name": "exec", "input": "text(1 + 1);"},
        {"type": "function_call", "call_id": "fn", "name": "exec_command", "arguments": '{"a": 1}'},
    ]
    lines = [
        json.dumps({"timestamp": "2026-08-23T11:00:03.000Z", "type": "response_item", "payload": c})
        for c in calls
    ]
    formats = {
        fact.call_id: fact.details[0].format
        for fact in parse_facts(CodexObserver(), lines)
        if fact.kind is TrajectoryKind.TOOL_CALL
    }

    assert formats == {"js": ContentFormat.CODE, "fn": ContentFormat.JSON}


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
    monkeypatch,
) -> None:
    path = tmp_path / "rollout.jsonl"
    records = [
        {
            "type": "session_meta",
            "payload": {
                "cwd": str(tmp_path),
                "model_provider": "azure",
                "thread_source": "user",
                "source": "cli",
            },
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
    seed_calls = []
    original_seed = source._observer._seed_history_context

    def record_seed(fh, start):
        seed_calls.append(source._observer)
        original_seed(fh, start)

    monkeypatch.setattr(source._observer, "_seed_history_context", record_seed)

    page = await source.history_page(limit=20)
    assert seed_calls == [source._observer]
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

    def unreadable_seed(fh, start):
        raise OSError("seed read failed")

    monkeypatch.setattr(source._observer, "_seed_history_context", unreadable_seed)
    failed = await source.history_page(limit=20)
    assert failed.error_code == "transcript_source_unavailable"


async def test_codex_live_attachment_seeds_model_context_for_usage(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    records = [
        {
            "type": "session_meta",
            "payload": {
                "cwd": str(tmp_path),
                "model_provider": "openai",
                "thread_source": "user",
                "source": "cli",
            },
        },
        {
            "type": "turn_context",
            "payload": {"turn_id": "turn-1", "model": "gpt-5.6-sol"},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "UserMessage",
                    "content": [{"type": "text", "text": "work"}],
                },
            },
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


def test_codex_never_projects_encrypted_only_reasoning_and_keeps_unmatched_results():
    facts = parse_facts(CodexObserver(), fixture_lines("trajectory_codex.jsonl"))
    unmatched = next(fact for fact in facts if fact.call_id == "unmatched-call")

    assert [fact.summary for fact in facts if fact.kind is TrajectoryKind.REASONING] == [
        "explicit summary"
    ]
    assert unmatched.kind is TrajectoryKind.TOOL_RESULT
    assert unmatched.native_id is None
    assert unmatched.parent_call_id == "unmatched-call"
    assert unmatched.status is TrajectoryStatus.COMPLETED


def test_codex_missing_message_id_uses_fallback_coordinates():
    lines = fixture_lines("trajectory_codex.jsonl")
    facts = parse_facts(CodexObserver(), lines)
    fallback = next(fact for fact in facts if fact.summary == "fallback")

    assert fallback.native_id is None
    assert fallback.raw_index == 11
    assert fallback.event_ordinal == 0
