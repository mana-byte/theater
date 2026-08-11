"""Tests for the régie bus view formatting."""

from __future__ import annotations

from theater.regie.bus_view import format_bus_line


def test_agent_assistant_event_has_cyan_kind():
    row = {
        "id": 1,
        "ts": 1723000000,
        "kind": "agent.assistant",
        "from_id": "abc123",
        "to_id": None,
        "payload": {"text": "hello world", "tool": None, "ts": None, "turn_end": False, "index": 0},
    }
    text = format_bus_line(row)
    assert "agent.assistant" in str(text)
    assert "hello world" in str(text)
    assert "abc123" in str(text)


def test_tool_call_event_includes_tool_name():
    row = {
        "id": 2,
        "ts": 1723000001,
        "kind": "agent.tool_call",
        "from_id": "abc123",
        "to_id": None,
        "payload": {"text": None, "tool": "bash", "ts": None, "turn_end": False, "index": 1},
    }
    text = format_bus_line(row)
    assert "[bash]" in str(text)


def test_turn_end_is_marked():
    row = {
        "id": 3,
        "ts": 1723000002,
        "kind": "agent.assistant",
        "from_id": "abc123",
        "to_id": None,
        "payload": {"text": "done", "tool": None, "ts": None, "turn_end": True, "index": 2},
    }
    text = format_bus_line(row)
    assert "(turn end)" in str(text)


def test_participant_created_shows_payload():
    row = {
        "id": 4,
        "ts": 1723000003,
        "kind": "participant.created",
        "from_id": None,
        "to_id": "def456",
        "payload": {"tier": "spawned", "harness": "vibe", "cwd": "/tmp"},
    }
    text = format_bus_line(row)
    assert "participant.created" in str(text)
    assert "def456" in str(text)
    assert "spawned" in str(text)


def test_from_to_routing_shown():
    row = {
        "id": 5,
        "ts": 1723000004,
        "kind": "agent.user",
        "from_id": "abc123",
        "to_id": "def456",
        "payload": {"text": "do the thing", "tool": None, "ts": None, "turn_end": False, "index": 0},
    }
    text = format_bus_line(row)
    assert "abc123 -> def456" in str(text)
