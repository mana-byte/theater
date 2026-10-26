"""Fixtures for the Pilot-driven app tests."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

#: How much longer a sweep lasts in these tests than in the app.
SWEEP_STRETCH = 4


@pytest.fixture(autouse=True)
def stretched_sweeps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run sweeps on a slowed clock: tests poll a state that lasts half a second, and a CI runner
    that stalls for that long never shows the sweep at all."""
    monkeypatch.setattr(
        "regie.motions.wipe.time",
        SimpleNamespace(monotonic=lambda: time.monotonic() / SWEEP_STRETCH),
    )
