"""Theme-aware formatting for the optional diagnostic bus panel."""

from __future__ import annotations

from collections.abc import Mapping

from rich.text import Text

from regie.formatting import event_stamp, event_summary, event_who

_BUS_KIND_ROLES = {
    "agent.user": "success",
    "agent.assistant": "primary",
    "agent.tool_call": "warning",
    "agent.tool_result": "secondary-lighten-2",
    "agent.error": "error",
    "job.await.start": "secondary-lighten-2",
    "job.await.end": "secondary-lighten-2",
    "participant.created": "accent",
    "participant.hello": "accent",
    "participant.pane": "accent",
    "participant.status": "accent",
    "participant.dead": "error",
}

_FALLBACK = {
    "success": "green",
    "primary": "cyan",
    "warning": "yellow",
    "secondary-lighten-2": "blue",
    "error": "red",
    "accent": "magenta",
}


def format_bus_line(
    row: Mapping[str, object],
    width: int = 100,
    variables: Mapping[str, str] | None = None,
) -> Text:
    del width
    kind_value = row.get("kind")
    kind = kind_value if isinstance(kind_value, str) else "?"
    timestamp = row.get("ts")
    stamp = event_stamp(float(timestamp) if isinstance(timestamp, int | float) else None)
    line = Text()
    line.append(f"{stamp}  ", style="dim")
    line.append(f"{kind:<18} ", style=kind_style(kind, variables))
    line.append(f"{event_who(row)[:24]:<24} ", style="dim")
    line.append(event_summary(row.get("payload")))
    return line


def kind_style(kind: str, variables: Mapping[str, str] | None = None) -> str:
    """Resolve semantic roles through the active theme with safe fallbacks."""
    role = _BUS_KIND_ROLES.get(kind)
    if role is None:
        return "default"
    if variables is None:
        return _FALLBACK[role]
    return variables.get(role) or _FALLBACK[role]


__all__ = ["format_bus_line", "kind_style"]
