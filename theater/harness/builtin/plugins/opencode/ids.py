"""OpenCode 2.x identifiers, minted as schema/src/identifier.ts does: 6 time bytes + 14 base62."""

from __future__ import annotations

import secrets
import threading
import time

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_RANDOM_CHARS = 14
_TIME_MASK = 0xFFFF_FFFF_FFFF


class _Clock:
    """Millisecond time plus a per-millisecond counter, so ids minted together stay ordered."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_ms = 0
        self._counter = 0

    def tick(self) -> int:
        with self._lock:
            now = int(time.time() * 1000)
            if now != self._last_ms:
                self._last_ms, self._counter = now, 0
            self._counter += 1
            return now * 0x1000 + self._counter


_clock = _Clock()


def _mint(prefix: str, *, descending: bool) -> str:
    value = _clock.tick()
    head = ((~value if descending else value) & _TIME_MASK).to_bytes(6, "big").hex()
    tail = "".join(secrets.choice(_ALPHABET) for _ in range(_RANDOM_CHARS))
    return f"{prefix}_{head}{tail}"


def session_id() -> str:
    """Newest sorts first, like the sessions 2.x mints itself."""
    return _mint("ses", descending=True)


def message_id() -> str:
    return _mint("msg", descending=False)


__all__ = ["message_id", "session_id"]
