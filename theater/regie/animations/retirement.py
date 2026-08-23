"""Keyed reverse-reveal animation for agent-spawned participant leaves."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass

from theater.constants.regie import REGIE_NEW_LEAF_REVEAL_COLUMNS_PER_FRAME
from theater.regie.render.layout import Key


@dataclass(frozen=True, slots=True)
class LeafRetirementChange:
    """Leaves that began retiring or returned before retirement completed."""

    retire: frozenset[Key]
    restore: frozenset[Key]


@dataclass(frozen=True, slots=True)
class LeafRetirementFrame:
    """Visible widths for retiring leaves and leaves ready to unmount."""

    widths: dict[Key, int]
    completed: frozenset[Key]
    active: bool


class LeafRetirementController:
    """Retire leaves created by agents while preserving their provenance."""

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled
        self._active: dict[Key, bool] = {}
        self._retiring: dict[Key, bool] = {}
        self._pending: dict[Key, int] = {}

    @property
    def active(self) -> bool:
        return bool(self._pending)

    def observe(self, current: Mapping[Key, bool]) -> LeafRetirementChange:
        """Compare active keys and preserve each leaf's original parent provenance."""
        current_keys = set(current)
        retire: set[Key] = set()
        restore: set[Key] = set()

        for key in set(self._active) - current_keys:
            eligible = self._active.pop(key)
            if self._enabled and eligible:
                self._retiring[key] = eligible
                retire.add(key)

        for key in current_keys:
            if key in self._retiring:
                self._retiring.pop(key)
                self._pending.pop(key, None)
                self._active[key] = True
                restore.add(key)
            elif key not in self._active:
                self._active[key] = current[key]

        return LeafRetirementChange(frozenset(retire), frozenset(restore))

    def begin(
        self, widths: Mapping[Key, int], *, candidates: Collection[Key] = ()
    ) -> LeafRetirementFrame:
        """Start mounted candidates and discard candidates that supplied no width."""
        completed: set[Key] = set()
        for key in set(candidates) - set(widths):
            self._retiring.pop(key, None)
            self._pending.pop(key, None)
        for key, width in widths.items():
            if width > 0:
                self._pending[key] = width
            else:
                self._retiring.pop(key, None)
                completed.add(key)
        return LeafRetirementFrame(dict(self._pending), frozenset(completed), self.active)

    def tick(self) -> LeafRetirementFrame:
        """Shrink each pending leaf by the existing agent-new-leaf cadence."""
        completed: set[Key] = set()
        widths: dict[Key, int] = {}
        for key, width in list(self._pending.items()):
            next_width = max(0, width - REGIE_NEW_LEAF_REVEAL_COLUMNS_PER_FRAME)
            widths[key] = next_width
            if next_width == 0:
                self._pending.pop(key)
                self._retiring.pop(key, None)
                completed.add(key)
            else:
                self._pending[key] = next_width
        return LeafRetirementFrame(widths, frozenset(completed), self.active)

    def clear(self) -> None:
        """Discard transient state during application teardown."""
        self._active.clear()
        self._retiring.clear()
        self._pending.clear()
