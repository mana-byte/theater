"""Provider screen-result mechanics only."""

from __future__ import annotations

from theater.harness import clip


def screen_result(capture: str) -> str:
    """What a screen-derived turn end can offer a waiting caller as a result.

    The visible pane with its trailing prompt line removed. This is not the
    agent's answer: it is one screenful of rendering, banner and all, cut off
    at the top by the pane height and stripped of everything that scrolled
    past. It is the best available for a harness with no transcript, and the
    thinness of it is the price of declaring a harness instead of writing a
    plugin that can read one.
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
