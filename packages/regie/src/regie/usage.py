"""Independent public usage reads for Régie's presentation footer."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from theater.frontend import FrontendClient


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    totals: Mapping[str, object]
    summary: Mapping[str, object]
    by_harness: Mapping[str, object]


class UsageController:
    """Read usage through the public API without retaining daemon-side accounting state."""

    def __init__(self, client: FrontendClient) -> None:
        self._client = client
        self._snapshot: UsageSnapshot | None = None
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> UsageSnapshot | None:
        return self._snapshot

    async def refresh(self, *, window: str) -> UsageSnapshot:
        async with self._lock:
            since = calendar_period_since(window)
            summary = (await self._client.usage.summary(since=since)).value
            plain_summary = _plain_mapping(summary)
            windowed = plain_summary.get("windowed")
            self._snapshot = UsageSnapshot(
                _plain_mapping(windowed) if isinstance(windowed, Mapping) else {},
                plain_summary,
                {},
            )
            return self._snapshot

    async def breakdown(self) -> dict[str, object]:
        async with self._lock:
            value = (await self._client.usage.by_harness(since=None)).value
            return _plain_mapping(value)

    async def detailed_breakdown(self) -> dict[str, object]:
        async with self._lock:
            value = (await self._client.usage.by_harness(since=None, detailed=True)).value
            return _plain_mapping(value)


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    return value


def _plain_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("usage response must be a mapping")
    return {str(key): _plain(item) for key, item in value.items()}


def calendar_period_since(window: str, *, at: datetime | None = None) -> float:
    """Return the local-calendar boundary matching Régie's period label."""
    current = datetime.now() if at is None else at
    today = current.astimezone().date() if current.tzinfo is not None else current.date()
    if window == "year":
        boundary = date(today.year, 1, 1)
    elif window == "month":
        boundary = date(today.year, today.month, 1)
    elif window == "week":
        boundary = today - timedelta(days=today.weekday())
    else:
        boundary = today
    # Localise the boundary itself.  ``datetime.now().astimezone()`` exposes
    # a fixed-offset timezone on macOS, so replacing fields on that value can
    # incorrectly carry today's CEST offset into a January boundary.
    return datetime.combine(boundary, time.min).astimezone().timestamp()


__all__ = ["UsageController", "UsageSnapshot", "calendar_period_since"]
