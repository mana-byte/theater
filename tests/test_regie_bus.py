"""Tests for the régie bus view formatting."""

from __future__ import annotations

from theater.regie import bus_view
from theater.regie.bus_view import format_bus_line, kind_style


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
        "payload": {
            "text": "do the thing",
            "tool": None,
            "ts": None,
            "turn_end": False,
            "index": 0,
        },
    }
    text = format_bus_line(row)
    assert "abc123 -> def456" in str(text)


# ---- colour follows the theme -------------------------------------------


def styles(text) -> list[str]:
    return [str(span.style) for span in text.spans]


def test_without_a_theme_the_original_palette_is_used():
    """The fallback is what this module shipped with, not a guess."""
    assert kind_style("agent.user") == "green"
    assert kind_style("participant.dead") == "red"


def test_a_theme_variable_replaces_the_literal_colour():
    variables = {"success": "#A3BE8C"}
    assert kind_style("agent.user", variables) == "#A3BE8C"


def test_two_themes_give_two_colours_for_the_same_kind():
    """The whole point: the panel stops being Textual-dark-coloured forever."""
    nord = kind_style("agent.assistant", {"primary": "#88C0D0"})
    gruvbox = kind_style("agent.assistant", {"primary": "#85A598"})
    assert nord != gruvbox


def test_a_theme_missing_a_slot_falls_back_rather_than_crashing():
    assert kind_style("agent.user", {"primary": "#88C0D0"}) == "green"


def test_an_unknown_kind_is_left_plain():
    """A new event type should read as ordinary, not as an error."""
    assert kind_style("something.new") == "default"
    assert kind_style("something.new", {"error": "#FF0000"}) == "default"


def test_every_role_the_kinds_use_has_a_fallback():
    """A role with no fallback would KeyError the first time it rendered."""
    assert set(bus_view._BUS_KIND_ROLES.values()) <= set(bus_view._FALLBACK)


def test_the_rendered_line_carries_the_theme_colour():
    row = {
        "id": 6,
        "ts": 1723000005,
        "kind": "agent.user",
        "from_id": "abc123",
        "to_id": None,
        "payload": {"text": "hi", "tool": None, "ts": None, "turn_end": False, "index": 0},
    }
    assert "#A3BE8C" in styles(format_bus_line(row, variables={"success": "#A3BE8C"}))


def test_the_timestamp_stays_dim_under_any_theme():
    """Scaffolding is not themed; only the event kind is."""
    row = {
        "id": 7,
        "ts": 1723000006,
        "kind": "agent.user",
        "from_id": "abc123",
        "to_id": None,
        "payload": {"text": "hi", "tool": None, "ts": None, "turn_end": False, "index": 0},
    }
    assert "dim" in styles(format_bus_line(row, variables={"success": "#A3BE8C"}))
