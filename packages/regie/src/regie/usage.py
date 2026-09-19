"""Independent public usage reads for Régie's presentation footer."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass

from theater.frontend import FrontendClient

_WINDOW_SECONDS = {"day": 86_400.0, "week": 604_800.0, "month": 2_592_000.0, "year": 31_536_000.0}


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

    @property
    def snapshot(self) -> UsageSnapshot | None:
        return self._snapshot

    async def refresh(self, *, window: str) -> UsageSnapshot:
        seconds = _WINDOW_SECONDS.get(window, _WINDOW_SECONDS["day"])
        since = time.time() - seconds
        totals = (await self._client.usage.totals(since=since)).value
        summary = (await self._client.usage.summary(since=since)).value
        by_harness = (await self._client.usage.by_harness(since=None)).value
        self._snapshot = UsageSnapshot(
            _plain_mapping(totals),
            _plain_mapping(summary),
            _plain_mapping(by_harness),
        )
        return self._snapshot

    async def detailed_breakdown(self) -> dict[str, object]:
        value = (await self._client.usage.by_harness(since=None, detailed=True)).value
        return _plain_mapping(value)


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    return value


def _plain_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return {str(key): _plain(item) for key, item in value.items()}


__all__ = ["UsageController", "UsageSnapshot"]
