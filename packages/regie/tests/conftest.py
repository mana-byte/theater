"""Fixtures shared by the Régie test tree."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

import pytest
from textual.app import App

from packages.regie.tests.support.waiting import ui_state


@pytest.fixture
def fast_count_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tick cost count-ups four times faster: the same 20 frames and values, less waiting."""
    monkeypatch.setattr("regie.widgets.leaf.REGIE_FOOTER_ANIM_INTERVAL", 0.025)
    monkeypatch.setattr("regie.widgets.animation_clock.REGIE_ANIMATION_CLOCK_INTERVAL", 0.025)


@pytest.fixture(autouse=True)
def ui_state_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Note the live UI state on any exception raised inside ``App.run_test``.

    The app is unmounted by the time the report is made, so the state has to be captured
    while it is still running; every wait_until runs inside run_test, so this prints once.
    """
    original = App.run_test

    @asynccontextmanager
    async def run_test_with_ui_state(self: App, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async with original(self, *args, **kwargs) as pilot:
            try:
                yield pilot
            except Exception as exc:
                # A diagnostic must never mask the real failure; the app is still live here.
                with suppress(Exception):
                    exc.add_note(ui_state(self))
                raise

    monkeypatch.setattr(App, "run_test", run_test_with_ui_state)
