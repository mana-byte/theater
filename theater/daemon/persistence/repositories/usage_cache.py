"""Exact summary reuse while neither cutoff crosses a recorded usage timestamp."""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass


def timezone_key() -> tuple:
    return os.environ.get("TZ"), time.tzname, time.timezone, time.altzone, time.daylight


@dataclass(frozen=True, slots=True)
class CutoffRange:
    excluded: float | None
    included: float | None

    def contains(self, cutoff: float) -> bool:
        return (
            math.isfinite(cutoff)
            and (self.excluded is None or cutoff > self.excluded)
            and (self.included is None or cutoff <= self.included)
        )


@dataclass(frozen=True, slots=True)
class SummaryCache:
    window: CutoffRange
    average: CutoffRange
    timezone: tuple
    values: dict[str, dict]

    def get(self, since: float, average_since: float) -> dict[str, dict] | None:
        if (
            self.timezone == timezone_key()
            and self.window.contains(since)
            and self.average.contains(average_since)
        ):
            return {key: dict(value) for key, value in self.values.items()}
        return None
