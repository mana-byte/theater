"""Independent initial-read and polling lifetimes for the frontend."""

from collections.abc import Awaitable, Callable


async def start_reader(
    initial: Callable[[], Awaitable[object]] | None,
    *,
    interval: float,
    poll: Callable[[], Awaitable[None]],
    start_timer: Callable[[float, Callable[[], Awaitable[None]]], object],
) -> None:
    """Start polling once this reader is ready, without racing its initial read."""
    if initial is not None:
        await initial()
    start_timer(interval, poll)
