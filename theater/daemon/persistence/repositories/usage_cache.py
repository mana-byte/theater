"""Exact summary reuse while neither cutoff crosses a recorded usage timestamp.

Inserts update the cached totals in place rather than discarding them: a full scan of the usage
table blocks the daemon's event loop for tens of milliseconds and would otherwise follow every
insert.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace


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


#: Outside 1970..2037 SQLite remaps the year before applying the local offset, so its local day can
#: differ from Python's; such timestamps drop the cache and are counted by the rescan.
_MIN_DATE_TS, _MAX_DATE_TS = 0.0, 2145916800.0


def local_date(ts: float) -> str:
    """The calendar day SQLite's ``date(ts, 'unixepoch', 'localtime')`` assigns to ``ts``."""
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _widened(cutoffs: CutoffRange, ts: float, included: bool) -> CutoffRange:
    if included:
        return CutoffRange(
            cutoffs.excluded, ts if cutoffs.included is None else min(cutoffs.included, ts)
        )
    return CutoffRange(
        ts if cutoffs.excluded is None else max(cutoffs.excluded, ts), cutoffs.included
    )


@dataclass(frozen=True, slots=True)
class SummaryCache:
    window: CutoffRange
    average: CutoffRange
    timezone: tuple
    values: dict[str, dict]
    since: float = 0.0
    average_since: float = 0.0
    active_dates: frozenset[str] = frozenset()

    def with_row(self, ts: float, columns: Mapping[str, int]) -> SummaryCache | None:
        """The summary after one more row, classified by the cutoffs it was computed with.

        ``None`` when ``ts`` is outside the range where Python and SQLite agree on the day.
        """
        if not _MIN_DATE_TS <= ts < _MAX_DATE_TS:
            return None
        values = {group: dict(totals) for group, totals in self.values.items()}
        in_window = ts >= self.since
        in_average = ts >= self.average_since
        for name, amount in columns.items():
            values["all_time"][name] += amount
            if in_window:
                values["windowed"][name] += amount
            if in_average:
                values["average"][name] += amount
        dates = self.active_dates | {local_date(ts)} if in_average else self.active_dates
        values["average"]["active_days"] = len(dates)
        return replace(
            self,
            window=_widened(self.window, ts, in_window),
            average=_widened(self.average, ts, in_average),
            values=values,
            active_dates=dates,
        )

    def get(self, since: float, average_since: float) -> dict[str, dict] | None:
        if (
            self.timezone == timezone_key()
            and self.window.contains(since)
            and self.average.contains(average_since)
        ):
            return {key: dict(value) for key, value in self.values.items()}
        return None
