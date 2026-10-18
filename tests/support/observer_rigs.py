"""Observer test doubles: canned screen readings, a waiting source, and event shorthands."""

from __future__ import annotations

from theater.daemon.observer import (
    Observer,
)
from theater.harness.base import Event, EventKind
from theater.harness.observation import (
    ScreenKind,
    ScreenReading,
)
from theater.harness.source import Batch, Source
from theater.models import (
    Status,
)


class ReadingObserver:
    """An observer whose `screen_reading` returns a canned `ScreenReading`.

    Implements `is_idle_screen` only to satisfy the ABC; the reducer never
    calls it when `screen_reading` is overridden.
    """

    has_transcript = True

    def __init__(self, reading: ScreenReading):
        self._reading = reading

    def is_idle_screen(self, capture: str) -> bool:
        return self._reading.kind is ScreenKind.PROMPT

    def screen_reading(self, capture: str) -> ScreenReading:
        return self._reading


def screen_checked(registry, *, reading: ScreenReading, status=Status.IDLE, **observer_options):
    """A participant and an observer whose screen returns the given reading.

    `_capture` is monkey-patched so no subprocess is spawned.
    """
    observer = Observer(registry, harnesses={}, **observer_options)
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    registry.set_status(p.id, status)

    async def capture_pane(_pane):
        return "$ "

    observer._capture = capture_pane
    return observer, ReadingObserver(reading), p


class WaitingSource(Source):
    """Never attaches. What a transcript that does not exist yet looks like."""

    def __init__(self) -> None:
        self.reads = 0

    async def read(self) -> Batch:
        self.reads += 1
        return Batch(waiting=True)

    async def aclose(self) -> None:
        return None


class WaitingHarness:
    """Carries an observer whose source waits forever and whose screen speaks."""

    binary = "waiting"

    def __init__(self, reading: ScreenReading):
        self.observer = ReadingObserver(reading)
        self.source = WaitingSource()
        self.observer.open_source = lambda **_: self.source  # type: ignore[method-assign]


def said(text: str, *, turn_end: bool) -> Event:
    return Event(kind=EventKind.ASSISTANT, text=text, turn_end=turn_end)
