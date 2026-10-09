"""One shared Textual timer driving every leaf animation.

Per-widget set_interval meant N spinning leaves woke the loop N times per
frame; the clock wakes it once, and subscribers advance on their own period.
Each keeps its requested period via a deadline, not a rounded tick divisor.
"""

from __future__ import annotations

import time
import weakref
from collections.abc import Callable
from typing import TYPE_CHECKING

from textual.timer import Timer

from regie.ui_constants import REGIE_ANIMATION_CLOCK_INTERVAL

if TYPE_CHECKING:
    from textual.app import App


def _displayed(callback: Callable[[], None]) -> bool:
    """Hidden owners keep their subscription; their frames resume once shown."""
    owner = getattr(callback, "__self__", None)
    return getattr(owner, "display", True)


class AnimationSubscription:
    """One subscriber's slot on the shared clock; stop() detaches it.

    The callback is held weakly, so a dead owner is dropped at the next tick
    even if it never unsubscribed.
    """

    __slots__ = ("_callback", "_clock", "due", "period")

    def __init__(
        self, clock: AnimationClock, period: float, due: float, callback: Callable[[], None]
    ) -> None:
        self._clock = clock
        self.period = period
        self.due = due
        self._callback = weakref.WeakMethod(callback)

    def stop(self) -> None:
        """Detach from the clock; safe to call more than once."""
        self._clock._remove(self)


class AnimationClock:
    """One timer, many cadences: each subscriber fires once its deadline passes."""

    def __init__(self, app: App, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        # Weak, or the registry below would keep the app alive forever.
        self._app = weakref.ref(app)
        self._timer: Timer | None = None
        self._subscriptions: list[AnimationSubscription] = []

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)

    @property
    def is_running(self) -> bool:
        return self._timer is not None

    def subscribe(self, period: float, callback: Callable[[], None]) -> AnimationSubscription:
        subscription = AnimationSubscription(self, period, self._now() + period, callback)
        self._subscriptions.append(subscription)
        if self._timer is None:
            app = self._require_app()
            self._timer = app.set_interval(REGIE_ANIMATION_CLOCK_INTERVAL, self._on_tick)
        return subscription

    def _require_app(self) -> App:
        app = self._app()
        if app is None:
            raise RuntimeError("animation clock outlived its app")
        return app

    def _remove(self, subscription: AnimationSubscription) -> None:
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)
        if not self._subscriptions:
            self._stop_timer()

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _on_tick(self) -> None:
        now = self._now()
        # Timer jitter: a tick a hair early still serves a deadline.
        horizon = now + REGIE_ANIMATION_CLOCK_INTERVAL / 10
        alive: list[AnimationSubscription] = []
        due: list[Callable[[], None]] = []
        for subscription in self._subscriptions:
            callback = subscription._callback()
            if callback is None:
                continue
            alive.append(subscription)
            if subscription.due > horizon:
                continue
            # Re-arm from the last deadline, not now, so the cadence never drifts.
            while subscription.due <= horizon:
                subscription.due += subscription.period
            if _displayed(callback):
                due.append(callback)
        self._subscriptions = alive
        if not due:
            if not alive:
                self._stop_timer()
            return
        with self._require_app().batch_update():
            for callback in due:
                callback()


_clocks: weakref.WeakKeyDictionary[App, AnimationClock] = weakref.WeakKeyDictionary()


def animation_clock(app: App) -> AnimationClock:
    """The per-app clock: one per running app, dropped when the app exits."""
    clock = _clocks.get(app)
    if clock is None:
        clock = AnimationClock(app)
        _clocks[app] = clock
    return clock


__all__ = ["AnimationClock", "AnimationSubscription", "animation_clock"]
