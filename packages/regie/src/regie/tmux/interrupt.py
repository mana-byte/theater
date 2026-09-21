"""Bounded daemon-planned interrupt sequences with per-key admission checks."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping

from regie.tmux.command import TmuxError, TmuxOutcomeUnknown, run


async def interrupt_terminal(
    pane_id: str,
    action: object,
    *,
    before_effect: Callable[[], None] | None = None,
    recheck: Callable[[], Awaitable[None]] | None = None,
) -> None:
    keys, delay = _sequence(action)
    if len(keys) > 1 and recheck is None:
        raise TmuxError("multi-key interruption requires fresh terminal admission")
    attempted = False
    try:
        for index, key in enumerate(keys):
            if index:
                if delay:
                    await asyncio.sleep(delay)
                assert recheck is not None
                await recheck()
            if before_effect is not None:
                before_effect()
            attempted = True
            await run("send-keys", "-t", pane_id, key)
    except Exception as exc:
        if attempted:
            raise TmuxOutcomeUnknown(
                "interrupt sequence may have partially executed; it must not be replayed"
            ) from exc
        raise


def _sequence(action: object) -> tuple[tuple[str, ...], float]:
    if isinstance(action, str):
        key = {"interrupt": "C-c", "escape": "Escape", "signal": "C-c"}.get(action)
        if key is not None:
            return (key,), 0.0
    if isinstance(action, Mapping):
        keys = action.get("keys")
        delay = action.get("inter_key_delay_seconds")
        if (
            isinstance(keys, (list, tuple))
            and 1 <= len(keys) <= 4
            and all(
                isinstance(key, str)
                and 1 <= len(key) <= 128
                and not key.startswith("-")
                and all(33 <= ord(char) <= 126 for char in key)
                for key in keys
            )
            and isinstance(delay, (int, float))
            and not isinstance(delay, bool)
            and math.isfinite(delay)
            and 0 <= delay <= 1
        ):
            return tuple(keys), float(delay)
    raise TmuxError("terminal interrupt action is not a bounded declared key sequence")
