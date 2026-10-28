"""Harness-neutral trajectory parsing: decode once, malformed lines, cost provenance and bounds."""

from __future__ import annotations

import json

import pytest

import theater.harness.builtin.plugins.claude.parser as claude_parser
import theater.harness.builtin.plugins.claude.usage as claude_plugin
import theater.harness.builtin.plugins.opencode as opencode_plugin
from tests.support.trajectory_parsing import fixture_lines
from theater.harness.builtin.plugins.claude.observer import ClaudeCodeObserver
from theater.harness.builtin.plugins.codex import parser as codex_plugin
from theater.harness.builtin.plugins.codex.observer import CodexObserver
from theater.harness.builtin.plugins.codex.values import _codex_usage
from theater.harness.contracts.trajectory import ParsedRecord
from theater.trajectory.enums import (
    CostProvenance,
    TrajectoryFailureCategory,
    TrajectoryStatus,
)


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
    lines = fixture_lines(fixture)
    control = observer_type()
    rich = observer_type()

    for index, line in enumerate(lines):
        assert list(rich.parse_record(line, index).events) == control.parse(line, index)


@pytest.mark.parametrize("observer_type", [ClaudeCodeObserver, CodexObserver])
def test_malformed_and_non_object_lines_are_empty(observer_type):
    observer = observer_type()
    for index, line in enumerate(("not-json", "null", "[]", "")):
        assert observer.parse_record(line, index) == ParsedRecord()


def test_cost_provenance_tracks_each_adapter_values() -> None:
    # Codex never reports a cost: its usage payloads carry token counts only.
    codex = _codex_usage({}, {"usage": {"input_tokens": 1}})
    opencode = opencode_plugin._opencode_usage({"tokens": {"input": 1}, "cost": 0.3})

    assert codex is not None
    assert codex.cost_usd is None and codex.cost_provenance is CostProvenance.UNKNOWN
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


def test_native_revision_is_independent_of_history_parse_order():
    lines = fixture_lines("trajectory_codex.jsonl")
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
