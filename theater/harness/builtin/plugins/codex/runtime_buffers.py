"""Paired terminal evidence and live preview buffers for one Codex runtime."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass

from theater.harness.contracts.runtime import NativeTurnOutcome

from .runtime_constants import (
    CODEX_RUNTIME_DELTA_ITEMS_MAX,
    CODEX_RUNTIME_DELTA_PREVIEW_MAX_CHARS,
    CODEX_RUNTIME_TERMINAL_TURNS_MAX,
)

_PENDING = object()


class TerminalOutcomeBuffer:
    """Reserve exact turns before loss-free enqueue and expose only accepted outcomes."""

    def __init__(self, maxsize: int) -> None:
        self._queue: asyncio.Queue[NativeTurnOutcome] = asyncio.Queue(maxsize=maxsize)
        self._buffered: dict[tuple[str, str], NativeTurnOutcome] = {}
        self._terminal_turns: OrderedDict[tuple[str, str], object] = OrderedDict()

    def reserve(self, session: str, turn_id: str) -> bool:
        key = (session, turn_id)
        if key in self._terminal_turns:
            return False
        self._terminal_turns[key] = _PENDING
        if len(self._terminal_turns) > CODEX_RUNTIME_TERMINAL_TURNS_MAX:
            self._terminal_turns.popitem(last=False)
        return True

    async def enqueue(self, outcome: NativeTurnOutcome) -> None:
        key = (outcome.native_session_id, outcome.native_turn_id)
        try:
            await self._queue.put(outcome)
        except BaseException:
            # A cancelled insertion can be retried only while its reservation is pending.
            if self._terminal_turns.get(key) is _PENDING:
                del self._terminal_turns[key]
            raise
        self._terminal_turns[key] = None
        self._buffered[key] = outcome

    def drain(self) -> list[NativeTurnOutcome]:
        evidence: list[NativeTurnOutcome] = []
        while True:
            try:
                outcome = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._buffered.pop((outcome.native_session_id, outcome.native_turn_id))
            evidence.append(outcome)
        return evidence

    def snapshot(self) -> tuple[NativeTurnOutcome, ...]:
        return tuple(self._buffered.values())

    def has_more(self) -> bool:
        return not self._queue.empty()

    @property
    def queued_count(self) -> int:
        return self._queue.qsize()


@dataclass(frozen=True)
class DeltaPreview:
    item_id: str
    text: str


class PreviewBuffer:
    """Coalesce bounded deltas, retaining evicted IDs' last preview lengths."""

    def __init__(self) -> None:
        self._items: OrderedDict[str, str] = OrderedDict()
        self._previewed_chars: dict[str, int] = {}

    def append(self, item_id: str, delta: str) -> int | None:
        buffer = self._items.get(item_id)
        dropped = 0
        if buffer is None:
            if len(self._items) >= CODEX_RUNTIME_DELTA_ITEMS_MAX:
                self._items.popitem(last=False)
                dropped = 1
            buffer = ""
        if len(buffer) >= CODEX_RUNTIME_DELTA_PREVIEW_MAX_CHARS:
            return None
        buffer += delta
        if len(buffer) > CODEX_RUNTIME_DELTA_PREVIEW_MAX_CHARS:
            buffer = buffer[:CODEX_RUNTIME_DELTA_PREVIEW_MAX_CHARS]
        self._items[item_id] = buffer
        return dropped

    def complete(self, item_id: str) -> None:
        self._items.pop(item_id, None)
        self._previewed_chars.pop(item_id, None)

    def drain(self) -> list[DeltaPreview]:
        previews: list[DeltaPreview] = []
        for item_id, text in list(self._items.items()):
            seen = self._previewed_chars.get(item_id, 0)
            if len(text) <= seen:
                continue
            self._previewed_chars[item_id] = len(text)
            previews.append(DeltaPreview(item_id, text))
        return previews
