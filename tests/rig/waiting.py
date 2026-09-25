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
