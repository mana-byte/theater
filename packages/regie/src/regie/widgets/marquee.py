"""Shared marquee subscription for rows with a scrolling text window."""

from __future__ import annotations

from regie.ui_constants import REGIE_LEAF_MARQUEE_INTERVAL
from regie.widgets.animation_clock import AnimationSubscription, animation_clock
from regie.widgets.renameable import RenameableRow


class MarqueeMixin(RenameableRow):
    _marquee_sub: AnimationSubscription | None
    _marquee_offset: int

    def _should_marquee(self) -> bool:
        raise NotImplementedError

    def _tick_marquee(self) -> None:
        if self._should_marquee():
            self._marquee_offset += 1
        else:
            self._stop_marquee()
        self.update(self._render_label(), layout=False)

    def _start_marquee(self) -> None:
        if self._marquee_sub is None:
            self._marquee_sub = animation_clock(self.app).subscribe(
                REGIE_LEAF_MARQUEE_INTERVAL, self._tick_marquee
            )

    def _stop_marquee(self) -> None:
        if self._marquee_sub is not None:
            self._marquee_sub.stop()
            self._marquee_sub = None
        self._marquee_offset = 0

    def _sync_marquee(self) -> None:
        if self._should_marquee():
            self._start_marquee()
        else:
            self._stop_marquee()

    def _restart_marquee(self) -> None:
        """Start the window over when focus or size changes."""
        self._stop_marquee()
        self.update(self._render_label(), layout=False)
        self._sync_marquee()
