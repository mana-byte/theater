"""Screen capture and result mechanics only.

The tmux ``capture-pane`` call and the text it yields for a waiting caller.
No status policy, no screen-reading dispatch — that lives in the reducer.
"""

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


async def capture_pane(pane: str) -> str | None:
    """The pane's rendered text, or None if it could not be read.

    Imported lazily so the tmux client is not on the import path of modules
    that never capture. The function is a standalone so tests can monkeypatch
    ``Observer._capture`` without touching tmux internals.
    """
    from theater.tmux import client as tmux

    try:
        return await tmux.run("capture-pane", "-p", "-t", pane, check=False)
    except Exception:
        return None
