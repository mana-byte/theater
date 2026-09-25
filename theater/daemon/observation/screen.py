"""Provider screen-result mechanics only."""

from __future__ import annotations

from theater.harness import clip


def screen_result(capture: str) -> str:
    """What a screen-derived turn end can offer a waiting caller as a result.

    Not the agent's answer: one pane-height screenful minus the prompt line — the price of
    declaring a harness with no transcript instead of writing a plugin that reads one.
    """
    lines = [line.rstrip() for line in capture.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        lines.pop()
    return "\n".join(lines).strip()


def end_turn_from_screen_text(capture: str) -> str:
    """Clipped assistant text for a screen-derived bus event."""
    return clip(screen_result(capture))
