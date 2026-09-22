"""Public transcript event compatibility without changing private MCP values."""

from __future__ import annotations


def public_transcript_event(event: dict[str, object]) -> dict[str, object]:
    terminal = event["turn_terminal"]
    return {**event, "turn_terminal": terminal is not None, "turn_outcome": terminal}
