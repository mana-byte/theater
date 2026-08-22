"""Keyed reveal state for initial and newly discovered régie leaves."""

from __future__ import annotations

import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass

from theater.constants.regie import (
    REGIE_EMPTY_TREE_KEY,
    REGIE_NEW_LEAF_REVEAL_COLUMNS_PER_FRAME,
    REGIE_STARTUP_REVEAL_COLUMNS_PER_FRAME,
    REGIE_STARTUP_REVEAL_INTERVAL_SECONDS,
    REGIE_STARTUP_REVEAL_MAX_LEAVES,
    REGIE_STARTUP_REVEAL_MAX_SECONDS,
    REGIE_STARTUP_REVEAL_STAGGER_FRAMES,
)
from theater.regie.render.layout import Key


@dataclass(frozen=True, slots=True)
class LeafRevealFrame:
    """Visible columns for pending keys and whether another tick is needed."""

    widths: dict[Key, int]
    active: bool


@dataclass(frozen=True, slots=True)
class _PendingReveal:
    origin_frame: int
    columns_per_frame: int
    deadline: float


class LeafRevealController:
    """Reveal initial and newly discovered leaves once per stable key."""

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled
        self._started = False
        self._seen: set[Key] = set()
        self._pending: dict[Key, _PendingReveal] = {}
        self._frame = 0

    @property
    def active(self) -> bool:
        return bool(self._pending)

    @property
    def started(self) -> bool:
        return self._started

    def needs_sync(self, keys: Collection[Key]) -> bool:
        """Whether current keys require widths or an animation-frame update."""
        return not self._started or self.active or any(key not in self._seen for key in keys)

    def sync_keys(self, keys: Collection[Key]) -> set[Key]:
        """Keys whose full widths are needed for the next controller update."""
        if not self._started:
            return set(keys)
        return set(self._pending) | {key for key in keys if key not in self._seen}

    def observe(self, required: Mapping[Key, int], *, now: float | None = None) -> LeafRevealFrame:
        """Register unseen keys and return the current reveal frame."""
        current = time.monotonic() if now is None else now
        keys = tuple(required)
        if not self._started:
            self._started = True
            self._seen.add(REGIE_EMPTY_TREE_KEY)
            self._seen.update(keys)
            if self._enabled and len(keys) <= REGIE_STARTUP_REVEAL_MAX_LEAVES:
                self._add_initial(keys, current)
        else:
            unseen = [key for key in keys if key not in self._seen]
            self._seen.update(unseen)
            if self._enabled:
                self._add_new(unseen, current)
        return self._result(required, current)

    def tick(self, required: Mapping[Key, int], *, now: float | None = None) -> LeafRevealFrame:
        """Advance all pending leaves by one frame."""
        if not self._pending:
            return LeafRevealFrame({}, False)
        self._frame += 1
        current = time.monotonic() if now is None else now
        return self._result(required, current)

    def _add_initial(self, keys: tuple[Key, ...], now: float) -> None:
        for index, key in enumerate(keys):
            delay = index * REGIE_STARTUP_REVEAL_STAGGER_FRAMES
            self._pending[key] = _PendingReveal(
                origin_frame=self._frame + delay,
                columns_per_frame=REGIE_STARTUP_REVEAL_COLUMNS_PER_FRAME,
                deadline=now
                + REGIE_STARTUP_REVEAL_MAX_SECONDS
                + delay * REGIE_STARTUP_REVEAL_INTERVAL_SECONDS,
            )

    def _add_new(self, keys: list[Key], now: float) -> None:
        capacity = max(0, REGIE_STARTUP_REVEAL_MAX_LEAVES - len(self._pending))
        for key in keys[:capacity]:
            if key == REGIE_EMPTY_TREE_KEY:
                continue
            self._pending[key] = _PendingReveal(
                origin_frame=self._frame,
                columns_per_frame=REGIE_NEW_LEAF_REVEAL_COLUMNS_PER_FRAME,
                deadline=now + REGIE_STARTUP_REVEAL_MAX_SECONDS,
            )

    def _result(self, required: Mapping[Key, int], now: float) -> LeafRevealFrame:
        widths: dict[Key, int] = {}
        for key, pending in list(self._pending.items()):
            target = required.get(key)
            width = max(0, self._frame - pending.origin_frame) * pending.columns_per_frame
            if target is None or now >= pending.deadline or width >= target:
                del self._pending[key]
            else:
                widths[key] = width
        return LeafRevealFrame(widths, bool(self._pending))
