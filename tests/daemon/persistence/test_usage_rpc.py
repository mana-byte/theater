"""Usage RPCs: summary windows, local-calendar boundaries and per-harness zero fill."""

from __future__ import annotations

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from theater.daemon import diagnostic_params, diagnostics
from theater.daemon.rpc import usage as usage_mod


@pytest.fixture
def paris_timezone(monkeypatch):
    """Run local-boundary assertions outside UTC, restoring libc's timezone."""
    previous = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Europe/Paris")
    time.tzset()
    yield
    if previous is None:
        monkeypatch.delenv("TZ", raising=False)
    else:
        monkeypatch.setenv("TZ", previous)
    time.tzset()


async def test_usage_summary_rpc_returns_all_three_windows(client, monkeypatch):
    timestamp = 2_000_000.0
    monkeypatch.setattr(diagnostic_params, "now", lambda: timestamp)
    result = await client.call("usage_summary", window=24.0)

    assert result["since"] == timestamp - 24.0 * 3600.0
    assert set(result) == {
        "since",
        "average_since",
        "period",
        "all_time",
        "windowed",
        "average",
    }
    assert result["period"] is None
    for group in ("all_time", "windowed"):
        assert result[group] == {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "reasoning_output_tokens": 0,
            "cost_microcents": 0,
        }
    assert result["average"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "reasoning_output_tokens": 0,
        "cost_microcents": 0,
        "active_days": 0,
    }


async def test_usage_summary_uses_local_calendar_period_boundaries(
    client, monkeypatch, paris_timezone
):
    zone = ZoneInfo("Europe/Paris")
    timestamp = datetime(2026, 8, 21, 15, 30, tzinfo=zone).timestamp()
    monkeypatch.setattr(diagnostic_params, "now", lambda: timestamp)

    expected = {
        "day": datetime(2026, 8, 21, tzinfo=zone).timestamp(),
        "week": datetime(2026, 8, 17, tzinfo=zone).timestamp(),
        "month": datetime(2026, 8, 1, tzinfo=zone).timestamp(),
        "year": datetime(2026, 1, 1, tzinfo=zone).timestamp(),
    }
    for period, since in expected.items():
        result = await client.call("usage_summary", window=1.0, period=period)
        assert result["since"] == since
        assert result["period"] == period

    unknown = await client.call("usage_summary", window=2.0, period="fortnight")
    assert unknown["since"] == timestamp - 2.0 * 3600.0
    assert unknown["period"] is None


async def test_usage_summary_local_midnight_is_dst_safe(client, monkeypatch, paris_timezone):
    zone = ZoneInfo("Europe/Paris")
    # Europe/Paris changes from +01:00 to +02:00 after midnight on this date.
    timestamp = datetime(2026, 3, 29, 12, 0, tzinfo=zone).timestamp()
    monkeypatch.setattr(diagnostic_params, "now", lambda: timestamp)

    result = await client.call("usage_summary", window=24.0, period="day")

    assert result["since"] == datetime(2026, 3, 29, tzinfo=zone).timestamp()


async def test_usage_summary_day_resets_all_footer_totals_at_midnight(
    client, daemon, monkeypatch, paris_timezone
):
    zone = ZoneInfo("Europe/Paris")
    midnight = datetime(2026, 8, 21, tzinfo=zone).timestamp()
    timestamp = datetime(2026, 8, 21, 15, 30, tzinfo=zone).timestamp()
    monkeypatch.setattr(diagnostic_params, "now", lambda: timestamp)
    base = {
        "participant_id": "p1",
        "tree_root_id": "p1",
        "model": "model",
        "harness": "codex",
        "reasoning_output_tokens": 0,
    }
    assert daemon.store.record_usage(
        **base,
        usage_key="yesterday",
        ts=midnight - 1,
        input_tokens=100,
        output_tokens=200,
        cache_creation_input_tokens=300,
        cache_read_input_tokens=400,
        cost_microcents=500,
    )
    assert daemon.store.record_usage(
        **base,
        usage_key="today",
        ts=midnight + 1,
        input_tokens=1,
        output_tokens=2,
        cache_creation_input_tokens=3,
        cache_read_input_tokens=4,
        cost_microcents=5,
    )

    result = await client.call("usage_summary", window=24.0, period="day")

    assert result["windowed"] == {
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_creation_input_tokens": 3,
        "cache_read_input_tokens": 4,
        "reasoning_output_tokens": 0,
        "cost_microcents": 5,
    }
    assert result["all_time"]["input_tokens"] == 101
    assert result["all_time"]["cost_microcents"] == 505


async def test_usage_by_harness_covers_week_crossing_month_and_zero_fills_plugins(
    client, daemon, monkeypatch, paris_timezone
):
    zone = ZoneInfo("Europe/Paris")
    timestamp = datetime(2026, 9, 1, 15, 30, tzinfo=zone).timestamp()
    week_start = datetime(2026, 8, 31, tzinfo=zone).timestamp()
    month_start = day_start = datetime(2026, 9, 1, tzinfo=zone).timestamp()
    monkeypatch.setattr(usage_mod, "now", lambda: timestamp)
    base = {
        "participant_id": "p1",
        "tree_root_id": "p1",
        "model": "model",
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    for key, harness, ts, tokens, cost in (
        ("monday", "codex", datetime(2026, 8, 31, 12, tzinfo=zone).timestamp(), 10, 100),
        ("tuesday", "codex", datetime(2026, 9, 1, 12, tzinfo=zone).timestamp(), 1, 10),
        ("plugin", "custom", datetime(2026, 9, 1, 13, tzinfo=zone).timestamp(), 2, 20),
        # A zero-token/cost row is still an active unattributed day.
        ("legacy", "unknown", datetime(2026, 9, 1, 14, tzinfo=zone).timestamp(), 0, 0),
    ):
        assert daemon.store.record_usage(
            **base,
            usage_key=key,
            harness=harness,
            ts=ts,
            input_tokens=tokens,
            output_tokens=tokens * 2,
            cost_microcents=cost,
        )

    result = await client.call("usage_by_harness")

    assert result["since"] == {
        "day": day_start,
        "week": week_start,
        "month": month_start,
    }
    rows = result["harnesses"]
    names = [row["harness"] for row in rows]
    assert names[-2:] == ["custom", "unknown"]
    codex = rows[names.index("codex")]
    assert codex["today"]["input_tokens"] == 1
    assert codex["week"]["input_tokens"] == 11
    assert codex["week"]["active_days"] == 2
    assert codex["month"]["input_tokens"] == 1
    vibe = rows[names.index("vibe")]
    assert all(value == 0 for value in vibe["today"].values())
    unknown = rows[-1]
    assert unknown["today"]["active_days"] == 1
    assert sum(row["week"]["input_tokens"] for row in rows) == 13
    assert set(result) == {"since", "harnesses"}
    assert all("models" not in row for row in rows)

    detailed = await client.call("usage_by_harness", detailed=True)

    assert set(detailed) == {"since", "harnesses", "totals"}
    detailed_rows = {row["harness"]: row for row in detailed["harnesses"]}
    assert detailed_rows["vibe"]["models"] == []
    assert detailed_rows["codex"]["models"][0]["model"] == "model"
    assert detailed["totals"]["week"]["active_days"] == 2
    assert detailed["totals"]["today"]["cost_microcents"] == 30


async def test_usage_by_harness_does_not_zero_fill_plugins_that_failed_to_load(client, monkeypatch):
    monkeypatch.setattr(
        diagnostics,
        "describe",
        lambda: [
            {"name": "working", "error": None},
            {"name": "broken", "error": "failed to import"},
        ],
    )

    result = await client.call("usage_by_harness")

    assert [row["harness"] for row in result["harnesses"]] == ["working"]
