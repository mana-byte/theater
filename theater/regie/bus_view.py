"""Format bus events for the régie's bus panel.

The bus panel shows scrolling inter-agent traffic — the same events as
`theater bus`, but rendered as Rich Text lines in a RichLog widget.
"""

from __future__ import annotations

import time

from rich.text import Text


def _summary(payload: dict | None) -> str:
    """One line describing an event, whatever kind it is."""
    if not payload:
        return ""
    bits = []
    if payload.get("tool"):
        bits.append(f"[{payload['tool']}]")
    if payload.get("text"):
        bits.append(" ".join(str(payload["text"]).split()))
    if not bits:
        import json

        known = {"ts", "turn_end", "index"}
        rest = {k: v for k, v in payload.items() if k not in known and v is not None}
        if rest:
            bits.append(json.dumps(rest, separators=(",", ":")))
    if payload.get("turn_end"):
        bits.append("(turn end)")
    return " ".join(bits)


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
    stamp = time.strftime("%H:%M:%S", time.localtime(row.get("ts") or 0))
    kind = str(row.get("kind", "?"))
    who = row.get("from_id") or "-"
    if row.get("to_id"):
        who = f"{who} -> {row['to_id']}"

    line = Text()
    line.append(f"{stamp}  ", style="dim")
    line.append(f"{kind:<18} ", style=_BUS_KIND_COLORS.get(kind, "white"))
    line.append(f"{who[:24]:<24} ", style="dim")
    summary = _summary(row.get("payload"))
    line.append(summary)
    return line
