"""The card under the tree: what the row in focus is, in a few dim lines."""

from __future__ import annotations

import textwrap
from collections.abc import Callable, Sequence

from rich.cells import cell_len
from textual.content import Content

from regie.formatting import format_cost, tilde
from regie.ui_constants import REGIE_AWAITED_MARK, REGIE_WAITING_FRAMES
from theater.frontend import Participant

_STATUS_STYLES = {"working": "$primary", "awaiting_input": "$warning", "dead": "$error"}

_DESCRIPTION_LINES = 2


def _head(text: str, width: int) -> str:
    """*text* cut to *width* cells, ending in ``…`` if it was cut."""
    if cell_len(text) <= width:
        return text
    while text and cell_len(text) > width - 1:
        text = text[:-1]
    return f"{text}…"


def _tail(text: str, width: int) -> str:
    """*text* cut to *width* cells from the left, starting with ``…``: a path keeps its end."""
    if cell_len(text) <= width:
        return text
    while text and cell_len(text) > width - 1:
        text = text[1:]
    return f"…{text}"


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes >= 24 * 60:
        return f"{minutes // (24 * 60)}d {minutes // 60 % 24}h"
    if minutes >= 60:
        return f"{minutes // 60}h {minutes % 60}m"
    return f"{minutes}m" if minutes else "<1m"


def _peers(glyph: str, text: str, names: Sequence[str], width: int) -> Content:
    return Content.assemble(
        (glyph, "$text-muted"), (_head(f" {text} {', '.join(names)}", width - 1), "$text-muted")
    )


def card_lines(
    participant: Participant,
    *,
    label_of: Callable[[str], str],
    cost_microcents: int | None,
    waiting_on: Sequence[str],
    waited_by: Sequence[str],
    now: float,
    width: int,
) -> list[Content]:
    """The facts about one agent the tree has no room for, one dim line each."""
    status = participant.status.replace("_", " ")
    name = _head(
        label_of(participant.participant_id),
        max(8, width - len(participant.harness) - 4 - len(status)),
    )
    lines = [
        Content.assemble(
            (name, "bold"),
            ("  ", ""),
            (participant.harness, "$text-muted"),
            ("  ", ""),
            (status, _STATUS_STYLES.get(participant.status, "$text-muted")),
        )
    ]
    if participant.cwd:
        lines.append(Content.assemble((_tail(tilde(participant.cwd), width), "$text-muted")))
    if participant.description:
        wrapped = textwrap.wrap(participant.description, width) or [""]
        shown = wrapped[:_DESCRIPTION_LINES]
        if len(wrapped) > _DESCRIPTION_LINES:
            shown[-1] = _head(f"{shown[-1]} …", width)
        lines.extend(Content.assemble((text, "$text")) for text in shown)
    facts = [
        *([format_cost(cost_microcents, decimals=2)] if cost_microcents else []),
        *([f"up {_duration(now - participant.created_at)}"] if participant.created_at else []),
    ]
    if facts:
        lines.append(Content.assemble((" · ".join(facts), "$text-muted")))
    if participant.parent_id:
        lines.append(
            Content.assemble(
                (_head(f"under {label_of(participant.parent_id)}", width), "$text-muted")
            )
        )
    if waiting_on:
        lines.append(
            _peers(REGIE_WAITING_FRAMES[0], "waiting on", [label_of(i) for i in waiting_on], width)
        )
    if waited_by:
        lines.append(
            _peers(REGIE_AWAITED_MARK, "waited on by", [label_of(i) for i in waited_by], width)
        )
    return lines


__all__ = ["card_lines"]
