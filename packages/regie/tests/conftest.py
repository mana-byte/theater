"""Fixtures shared by the Régie test tree."""

from __future__ import annotations

import pytest


@pytest.fixture
def fast_count_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tick cost count-ups four times faster: the same 20 frames and values, less waiting."""
    monkeypatch.setattr("regie.widgets.leaf.REGIE_FOOTER_ANIM_INTERVAL", 0.025)
    monkeypatch.setattr("regie.widgets.animation_clock.REGIE_ANIMATION_CLOCK_INTERVAL", 0.025)
