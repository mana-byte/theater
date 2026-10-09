"""The shared animation clock: one timer for every leaf spinner, marquee, cost tick."""

from __future__ import annotations

import contextlib
import gc
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

from regie.ui_constants import (
    REGIE_ANIMATION_CLOCK_INTERVAL,
    REGIE_LEAF_MARQUEE_INTERVAL,
    REGIE_LEAF_SPINNER_INTERVAL,
)
from regie.widgets import ParticipantTree
from regie.widgets.animation_clock import AnimationClock, animation_clock
from regie.widgets.leaf import AgentLeaf

from packages.regie.tests.test_ui import _app, _participant, _projection
from tests.rig.waiting import wait_until


class _StubTimer:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class _StubApp:
    """Just enough App for a clock driven by hand instead of a real event loop."""

    def __init__(self) -> None:
        self.timers: list[_StubTimer] = []
        self.intervals: list[float] = []

    def set_interval(self, interval: float, _callback) -> _StubTimer:
        timer = _StubTimer()
        self.timers.append(timer)
        self.intervals.append(interval)
        return timer

    def batch_update(self):
        @contextlib.contextmanager
        def _batch():
            yield

        return _batch()


class _Ticker:
    def __init__(self, display: bool = True) -> None:
        self.fires = 0
        self.display = display

    def tick(self) -> None:
        self.fires += 1


def test_clock_preserves_each_cadence_and_sleeps_when_idle() -> None:
    assert REGIE_ANIMATION_CLOCK_INTERVAL == REGIE_LEAF_SPINNER_INTERVAL
    app = _StubApp()
    clock = AnimationClock(app)
    assert not clock.is_running
    spinner = _Ticker()
    marquee = _Ticker()
    clock.subscribe(REGIE_LEAF_SPINNER_INTERVAL, spinner.tick)
    assert clock.is_running and app.intervals == [REGIE_ANIMATION_CLOCK_INTERVAL]
    marquee_sub = clock.subscribe(REGIE_LEAF_MARQUEE_INTERVAL, marquee.tick)

    # The marquee rides the shared 0.1 s tick, ~20% faster than its 0.12 s period.
    for _ in range(6):
        clock._on_tick()
    assert spinner.fires == 6
    assert marquee.fires == 6

    del spinner
    gc.collect()
    clock._on_tick()  # a dead owner is dropped, never ticked
    assert clock.subscriber_count == 1
    marquee_sub.stop()
    assert not clock.is_running and app.timers[0].stopped

    kept = _Ticker()
    resubscribed = clock.subscribe(REGIE_LEAF_SPINNER_INTERVAL, kept.tick)
    assert clock.is_running and len(app.timers) == 2
    resubscribed.stop()
    assert not clock.is_running

    hidden = _Ticker(display=False)
    clock.subscribe(REGIE_LEAF_SPINNER_INTERVAL, hidden.tick)
    clock._on_tick()
    assert hidden.fires == 0  # a hidden owner is skipped; frames resume once shown
    assert clock.subscriber_count == 1


async def test_working_leaves_share_one_timer_and_unmount_drains_it(tmp_path: Path) -> None:
    working = {
        f"worker-{index}": _participant(f"worker-{index}", name=f"worker-{index}", status="working")
        for index in range(5)
    }
    projection = replace(_projection(), participants=MappingProxyType(working))
    app, _client, _presentation = _app(
        tree_layout_path=tmp_path / "tree-layout.json", projection=projection
    )

    async with app.run_test() as pilot:
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: len(tree.participant_ids) == 5)
        clock = animation_clock(app)
        leaves = list(app.query(AgentLeaf))
        assert len(leaves) == 5
        await wait_until(pilot, lambda: clock.subscriber_count == 5 and clock.is_running)
        # No leaf owns a Textual timer any more; the clock holds the only one.
        assert all(not leaf._timers for leaf in leaves)
        await wait_until(pilot, lambda: any(leaf._frame for leaf in leaves))

        for leaf in leaves:
            await leaf.remove()
        await wait_until(pilot, lambda: clock.subscriber_count == 0)
        assert not clock.is_running
