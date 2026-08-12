"""Format bus events for the régie's bus panel.

The bus panel shows scrolling inter-agent traffic — the same events as
`theater bus`, but rendered as Rich Text lines in a RichLog widget.
"""

from __future__ import annotations

from rich.text import Text

from theater.formatting import event_stamp, event_summary, event_who

_BUS_KIND_COLORS = {
    "agent.user": "green",
    "agent.assistant": "cyan",
    "agent.tool_call": "yellow",
    "agent.tool_result": "blue",
    "agent.error": "red",
    "participant.created": "magenta",
    "participant.hello": "magenta",
    "participant.pane": "magenta",
    "participant.status": "magenta",
    "participant.dead": "red",
}


def format_bus_line(row: dict, width: int = 100) -> Text:
    """Format a bus row as a Rich Text line for the bus panel."""
    kind = str(row.get("kind", "?"))
    who = event_who(row)

    line = Text()
    line.append(f"{event_stamp(row.get('ts'))}  ", style="dim")
    line.append(f"{kind:<18} ", style=_BUS_KIND_COLORS.get(kind, "white"))
    line.append(f"{who[:24]:<24} ", style="dim")
    line.append(event_summary(row.get("payload")))
    return line
