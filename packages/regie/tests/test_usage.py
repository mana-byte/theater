from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from types import SimpleNamespace
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from regie.controllers.usage import participant_costs
from regie.formatting import format_cost, format_tokens
from regie.usage import UsageController, calendar_period_since
from regie.widgets.usage_breakdown import UsageBreakdownPanel

from theater.frontend import CapabilityUnavailable, FrontendClient


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
        self.participant_value: object = {
            "since": None,
            "participants": [],
            "truncated": False,
        }
        self.participant_calls: list[dict[str, object]] = []

    async def summary(self, *, since: float) -> object:
        self.summary_since.append(since)
        return SimpleNamespace(value=self.summary_value)

    async def by_harness(self, *, since: float | None, detailed: bool = False) -> object:
        del since, detailed
        return SimpleNamespace(value=self.breakdown_value)

    async def by_participant(
        self,
        *,
        since: float | None,
        participant_ids: tuple[str, ...] | None = None,
        limit: int,
    ) -> object:
        self.participant_calls.append(
            {"since": since, "participant_ids": participant_ids, "limit": limit}
        )
        return SimpleNamespace(value=self.participant_value)

    async def totals(self, *, since: float) -> object:
        raise AssertionError(f"refresh issued the redundant totals request for {since}")


class _Client:
    def __init__(self) -> None:
        self.usage = _Usage()


@pytest.mark.asyncio
async def test_refresh_uses_one_summary_request_and_its_windowed_totals() -> None:
    client = _Client()
    controller = UsageController(cast(FrontendClient, client))

    snapshot = await controller.refresh(window="day", participant_ids=("p1", "p2"))

    assert len(client.usage.summary_since) == 1
    assert snapshot.totals == {"input_tokens": 12, "cost_microcents": 34}
    assert snapshot.summary["average"] == {"active_days": 1}
    assert snapshot.by_participant == {
        "since": client.usage.participant_calls[0]["since"],
        "participants": [],
        "truncated": False,
    }
    assert client.usage.participant_calls == [
        {
            "since": client.usage.summary_since[0],
            "participant_ids": ("p1", "p2"),
            "limit": 500,
        }
    ]


@pytest.mark.asyncio
async def test_refresh_reads_summary_and_participants_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    summary_started = asyncio.Event()
    participants_started = asyncio.Event()

    async def summary(*, since: float) -> object:
        del since
        summary_started.set()
        await participants_started.wait()
        return SimpleNamespace(value=client.usage.summary_value)

    async def by_participant(
        *, since: float | None, participant_ids: tuple[str, ...], limit: int
    ) -> object:
        del since, participant_ids, limit
        participants_started.set()
        await summary_started.wait()
        return SimpleNamespace(value=client.usage.participant_value)

    monkeypatch.setattr(client.usage, "summary", summary)
    monkeypatch.setattr(client.usage, "by_participant", by_participant)

    await asyncio.wait_for(
        UsageController(cast(FrontendClient, client)).refresh(window="day"), timeout=1
    )


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


@pytest.mark.asyncio
async def test_participant_usage_chunks_large_live_trees(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client()
    calls: list[tuple[str, ...]] = []

    async def by_participant(
        *, since: float | None, participant_ids: tuple[str, ...], limit: int
    ) -> object:
        del since
        assert limit == 500
        calls.append(participant_ids)
        return SimpleNamespace(
            value={
                "since": None,
                "participants": [
                    {"participant_id": participant_id, "cost_microcents": index}
                    for index, participant_id in enumerate(participant_ids)
                ],
                "truncated": False,
            }
        )

    monkeypatch.setattr(client.usage, "by_participant", by_participant)
    controller = UsageController(cast(FrontendClient, client))
    participant_ids = tuple(f"p-{index:03}" for index in range(501))

    snapshot = await controller.refresh(window="day", participant_ids=participant_ids)
    result = snapshot.by_participant

    assert [len(chunk) for chunk in calls] == [500, 1]
    assert len(result["participants"]) == 501
    assert result["truncated"] is False


def test_compact_participant_usage_formatting() -> None:
    assert format_tokens(999) == "999"
    assert format_tokens(12_400) == "12k"
    assert format_tokens(1_250_000) == "1.2M"
    assert format_tokens(1_250_000_000) == "1.2B"
    assert format_tokens(1_250_000_000_000) == "1.2T"
    assert format_tokens(1_250_000_000_000_000) == "1.2Q"
    assert format_tokens(1_250_000_000_000_000_000) == "1.2E"
    assert format_cost(42_000_000, decimals=2) == "$0.42"
    assert format_cost(125_000_000_000, decimals=2) == "$1.2k"

    table = UsageBreakdownPanel._top_participants_table(
        [
            {
                "participant_id": "participant-a",
                "name": "Arlequin",
                "harness": "codex",
                "input_tokens": 12_400,
                "output_tokens": 900,
                "cost_microcents": 42_000_000,
            }
        ]
    )
    assert table is not None
    assert table.title == "Top participants"
    assert [column.header for column in table.columns] == ["name", "harness", "in", "out", "cost"]
    assert [column._cells[0] for column in table.columns] == [
        "Arlequin",
        "codex",
        "12k",
        "900",
        "$0.420",
    ]


def test_participant_costs_ignores_malformed_rows() -> None:
    assert participant_costs(
        {
            "participants": [
                {"participant_id": "p1", "cost_microcents": 42},
                {"participant_id": "p2", "cost_microcents": "unknown"},
                {"cost_microcents": 10},
            ]
        }
    ) == {"p1": 42}


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


@pytest.mark.asyncio
async def test_refresh_keeps_harness_usage_when_the_daemon_predates_participant_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()

    async def unavailable(**_kwargs: object) -> object:
        raise CapabilityUnavailable("frontend.usage.by_participant requires public API 1.1")

    monkeypatch.setattr(client.usage, "by_participant", unavailable)
    snapshot = await UsageController(cast(FrontendClient, client)).refresh(
        window="day", participant_ids=("p1",)
    )

    assert snapshot.totals == {"input_tokens": 12, "cost_microcents": 34}
    assert snapshot.by_participant["participants"] == []
