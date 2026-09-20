from __future__ import annotations

import os
import time
from datetime import datetime
from types import SimpleNamespace
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from regie.usage import UsageController, calendar_period_since

from theater.frontend import FrontendClient


@pytest.fixture
def paris_timezone(monkeypatch: pytest.MonkeyPatch):
    """Run local-boundary assertions in a zone whose offset changes seasonally."""
    previous = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Europe/Paris")
    time.tzset()
    yield
    if previous is None:
        monkeypatch.delenv("TZ", raising=False)
    else:
        monkeypatch.setenv("TZ", previous)
    time.tzset()


class _Usage:
    def __init__(self) -> None:
        self.summary_since: list[float] = []
        self.summary_value: object = {
            "windowed": {"input_tokens": 12, "cost_microcents": 34},
            "average": {"active_days": 1},
        }
        self.breakdown_value: object = {"harnesses": []}

    async def summary(self, *, since: float) -> object:
        self.summary_since.append(since)
        return SimpleNamespace(value=self.summary_value)

    async def by_harness(self, *, since: float | None, detailed: bool = False) -> object:
        del since, detailed
        return SimpleNamespace(value=self.breakdown_value)

    async def totals(self, *, since: float) -> object:
        raise AssertionError(f"refresh issued the redundant totals request for {since}")


class _Client:
    def __init__(self) -> None:
        self.usage = _Usage()


@pytest.mark.asyncio
async def test_refresh_uses_one_summary_request_and_its_windowed_totals() -> None:
    client = _Client()
    controller = UsageController(cast(FrontendClient, client))

    snapshot = await controller.refresh(window="day")

    assert len(client.usage.summary_since) == 1
    assert snapshot.totals == {"input_tokens": 12, "cost_microcents": 34}
    assert snapshot.summary["average"] == {"active_days": 1}


@pytest.mark.asyncio
async def test_malformed_usage_responses_do_not_replace_the_last_snapshot() -> None:
    client = _Client()
    controller = UsageController(cast(FrontendClient, client))
    snapshot = await controller.refresh(window="day")

    client.usage.summary_value = []
    with pytest.raises(TypeError, match="usage response"):
        await controller.refresh(window="day")
    assert controller.snapshot is snapshot

    client.usage.breakdown_value = "not a mapping"
    with pytest.raises(TypeError, match="usage response"):
        await controller.breakdown()


def test_usage_periods_start_at_local_calendar_boundaries(paris_timezone: None) -> None:
    timezone = ZoneInfo("Europe/Paris")
    current = datetime(2026, 9, 19, 15, 47, 33, 123456, tzinfo=timezone)

    expected = {
        "day": datetime(2026, 9, 19, tzinfo=timezone),
        "week": datetime(2026, 9, 14, tzinfo=timezone),
        "month": datetime(2026, 9, 1, tzinfo=timezone),
        "year": datetime(2026, 1, 1, tzinfo=timezone),
    }
    assert {
        period: datetime.fromtimestamp(calendar_period_since(period, at=current), timezone)
        for period in expected
    } == expected


def test_usage_year_boundary_recomputes_the_winter_offset(paris_timezone: None) -> None:
    timezone = ZoneInfo("Europe/Paris")
    summer = datetime(2026, 9, 19, 15, 47, tzinfo=timezone)

    boundary = datetime.fromtimestamp(calendar_period_since("year", at=summer), timezone)

    assert summer.utcoffset() != boundary.utcoffset()
    assert boundary == datetime(2026, 1, 1, tzinfo=timezone)
