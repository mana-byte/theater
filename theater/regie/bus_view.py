"""Format bus events for the régie's bus panel.

The bus panel shows scrolling inter-agent traffic — the same events as
`theater bus`, but rendered as Rich Text lines in a RichLog widget.

Colour is chosen by *role*, not by hex. A RichLog holds Rich Text, which the
Textual stylesheet never touches, so `$accent` in a style string is a literal
that Rich would reject. The theme is therefore resolved here: each event kind
maps to a semantic slot every Textual theme defines, and the caller passes
`App.theme_variables` to have those slots turned into the running theme's
colours. Without it the panel would keep rendering the default palette's green
and cyan next to a Nord or Gruvbox frame.
"""

from __future__ import annotations

from collections.abc import Mapping

from rich.text import Text

from theater.formatting import event_stamp, event_summary, event_who

#: Event kind -> theme slot; kinds that share a meaning share a colour (accent, error).
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

#: Fallback colours for when no theme is available (tests, callers with no running app).
_FALLBACK = {
    "success": "green",
    "primary": "cyan",
    "warning": "yellow",
    "secondary-lighten-2": "blue",
    "error": "red",
    "accent": "magenta",
}

#: Timestamp and participant id are not themed; dim reads as scaffolding against any palette.
_MUTED = "dim"


def kind_style(kind: str, variables: Mapping[str, str] | None = None) -> str:
    """The colour for an event kind under the given theme variables.

    An unknown kind gets no colour rather than a guessed one: a new event type
    should look plain, not like an error.
    """
    role = _BUS_KIND_ROLES.get(kind)
    if role is None:
        return "default"
    if variables is None:
        return _FALLBACK[role]
    # Missing slot is not a crash: a dull line is better than a dead panel.
    return variables.get(role) or _FALLBACK[role]


def format_bus_line(
    row: dict,
    width: int = 100,
    variables: Mapping[str, str] | None = None,
) -> Text:
    """Format a bus row as a Rich Text line for the bus panel."""
    kind = str(row.get("kind", "?"))
    who = event_who(row)

    line = Text()
    line.append(f"{event_stamp(row.get('ts'))}  ", style=_MUTED)
    line.append(f"{kind:<18} ", style=kind_style(kind, variables))
    line.append(f"{who[:24]:<24} ", style=_MUTED)
    line.append(event_summary(row.get("payload")))
    return line
