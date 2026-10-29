"""Wait for a UI condition instead of sleeping a guessed time; CI runners are slow."""

from __future__ import annotations

from collections.abc import Callable

from regie.widgets import ParticipantTree
from textual.css.query import NoMatches
from textual.pilot import Pilot

#: Tree text lines shown in a timeout message; diagnostic only, never a decision.
_MAX_TREE_LINES = 28


def _failure(pilot: Pilot, waited: str) -> str:
    """What a timeout reports: the public participant-tree state, when there is one."""
    app = getattr(pilot, "app", None)
    tree_state = ""
    if app is not None:
        try:
            tree = app.query_one(ParticipantTree)
        except NoMatches:
            tree = None
        if tree is not None:
            rows = "\n".join(str(line[0]) for line in tree.tree_lines)
            shown = "\n".join(rows.splitlines()[:_MAX_TREE_LINES])
            tree_state = (
                f"tree: participant_ids={tree.participant_ids!r} "
                f"selected_key={tree.selected_key!r}\n{shown}"
            )
    return f"{waited}\n{tree_state}"


async def wait_until(pilot: Pilot, condition: Callable[[], bool], timeout: float = 30.0) -> None:
    """Let the app run until condition holds; fail with a clear message after timeout.

    The timeout is a hang backstop sized for slow CI runners: it returns as soon as the
    condition holds, so it only ever fires on a real hang, never on a merely loaded runner.
    """
    steps = int(timeout / 0.02)
    for _ in range(steps):
        if condition():
            return
        await pilot.pause(0.02)
    assert condition(), _failure(pilot, f"condition not met within {timeout}s")


async def press_until(
    pilot: Pilot, key: str, condition: Callable[[], bool], limit: int = 40
) -> None:
    """Press *key* until *condition* holds: how far a cursor has to travel is not the point."""
    for _ in range(limit):
        if condition():
            return
        await pilot.press(key)
    assert condition(), _failure(pilot, f"{key!r} x{limit} never made it so")
