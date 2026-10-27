"""Record the sweeps a tree starts, so a test need not catch one mid-flight."""

from __future__ import annotations

import pytest
from regie.motions.wipe import LeafWipeController


def recorded_sweeps(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Every sweep started from now on as (key, color, seconds); the sweep itself still runs."""
    sweeps: list[tuple] = []
    start = LeafWipeController.start

    def recording_start(self, key, color, *, seconds=None, **kwargs) -> None:
        sweeps.append((key, color, seconds))
        start(self, key, color, seconds=seconds, **kwargs)

    monkeypatch.setattr(LeafWipeController, "start", recording_start)
    return sweeps
