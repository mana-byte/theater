"""Wait for a UI condition instead of sleeping a guessed time; CI runners are slow."""

from __future__ import annotations

from collections.abc import Callable

from textual.pilot import Pilot


async def wait_until(pilot: Pilot, condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Let the app run until condition holds; fail with a clear message after timeout."""
    steps = int(timeout / 0.02)
    for _ in range(steps):
        if condition():
            return
        await pilot.pause(0.02)
    assert condition(), f"condition not met within {timeout}s"


async def press_until(
    pilot: Pilot, key: str, condition: Callable[[], bool], limit: int = 40
) -> None:
    """Press *key* until *condition* holds: how far a cursor has to travel is not the point."""
    for _ in range(limit):
        if condition():
            return
        await pilot.press(key)
    assert condition(), f"{key!r} x{limit} never made it so"
