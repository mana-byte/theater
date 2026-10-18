"""Store usage ledger: dedup, summaries, rolling windows and the summary cache."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

from theater.daemon.persistence.repositories import usage_cache


def test_usage_is_deduplicated_and_aggregated(store):
    values = {
        "participant_id": "p1",
        "tree_root_id": "p1",
        "usage_key": "session:message",
        "ts": 10.0,
        "model": "model",
        "harness": "codex",
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_creation_input_tokens": 3,
        "cache_read_input_tokens": 5,
        "reasoning_output_tokens": 2,
        "cost_microcents": 900,
    }

    assert store.record_usage(**values) is True
    assert store.record_usage(**values) is False
    assert store.usage_totals() == {
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_creation_input_tokens": 3,
        "cache_read_input_tokens": 5,
        "reasoning_output_tokens": 2,
        "cost_microcents": 900,
    }
    assert all(value == 0 for value in store.usage_totals(since=11.0).values())


def test_usage_summary_returns_all_footer_windows_in_one_result(store):
    base = {
        "participant_id": "p1",
        "tree_root_id": "p1",
        "model": "model",
        "harness": "codex",
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    for key, ts, tokens, cost in (
        ("old", 1.0, 1, 10),
        ("average", 20.0, 2, 20),
        ("windowed", 30.0, 4, 40),
    ):
        assert store.record_usage(
            **base,
            usage_key=key,
            ts=ts,
            input_tokens=tokens,
            output_tokens=tokens * 10,
            cost_microcents=cost,
        )

    summary = store.usage_summary(since=25.0, average_since=15.0)

    assert summary["all_time"]["input_tokens"] == 7
    assert summary["all_time"]["output_tokens"] == 70
    assert summary["all_time"]["cost_microcents"] == 70
    assert summary["windowed"]["input_tokens"] == 4
    assert summary["windowed"]["cost_microcents"] == 40
    assert summary["average"]["input_tokens"] == 6
    assert summary["average"]["cost_microcents"] == 60
    assert summary["average"]["active_days"] == 1


def test_usage_summary_counts_only_distinct_active_rolling_days(store):
    average_since = 1_000_000.0
    base = {
        "participant_id": "p1",
        "tree_root_id": "p1",
        "model": "model",
        "harness": "codex",
        "input_tokens": 1,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    for key, offset, cost in (
        ("older", -1.0, 100),
        ("bucket-0-a", 3_600.0, 0),
        ("bucket-0-b", 84_600.0, 0),
        ("bucket-1", 93_600.0, 200),
    ):
        assert store.record_usage(
            **base,
            usage_key=key,
            ts=average_since + offset,
            cost_microcents=cost,
        )

    summary = store.usage_summary(since=average_since, average_since=average_since)

    assert summary["average"]["active_days"] == 2
    assert summary["average"]["cost_microcents"] == 200


def test_usage_summary_rolling_window_can_span_thirty_one_calendar_dates(store):
    average_since = datetime(2026, 1, 1, 12).timestamp()
    base = {
        "participant_id": "p1",
        "tree_root_id": "p1",
        "model": "model",
        "harness": "codex",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "reasoning_output_tokens": 0,
        "cost_microcents": 0,
    }
    for day in range(31):
        assert store.record_usage(
            **base,
            usage_key=str(day),
            ts=(datetime(2026, 1, 1, 12) + timedelta(days=day)).timestamp(),
        )

    summary = store.usage_summary(since=average_since, average_since=average_since)

    # A rolling 30×24h interval begins and ends mid-day, so both partial
    # boundary dates can be active even though the label remains "30d".
    assert summary["average"]["active_days"] == 31


def test_usage_by_harness_uses_each_calendar_boundary_and_metric_semantics(store):
    week_since = datetime(2026, 8, 31).timestamp()
    month_since = day_since = datetime(2026, 9, 1).timestamp()
    base = {
        "participant_id": "p1",
        "tree_root_id": "p1",
        "model": "model",
        "harness": "codex",
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    assert store.record_usage(
        **base,
        usage_key="monday",
        ts=datetime(2026, 8, 31, 12).timestamp(),
        input_tokens=10,
        output_tokens=20,
        cost_microcents=30,
    )
    assert store.record_usage(
        **base,
        usage_key="tuesday",
        ts=datetime(2026, 9, 1, 12).timestamp(),
        input_tokens=1,
        output_tokens=2,
        cost_microcents=3,
    )

    [row] = store.usage_by_harness(
        day_since=day_since,
        week_since=week_since,
        month_since=month_since,
    )

    assert row["today"]["input_tokens"] == 1
    assert row["today"]["active_days"] == 1
    assert row["week"]["input_tokens"] == 11
    assert row["week"]["output_tokens"] == 22
    assert row["week"]["cost_microcents"] == 33
    assert row["week"]["active_days"] == 2
    assert row["month"]["input_tokens"] == 1


def test_usage_by_participant_filters_ids_and_since_in_one_aggregate(store):
    base = {
        "tree_root_id": "root",
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    for participant_id, key, timestamp, model, input_tokens, output_tokens, cost in (
        ("p1", "p1-old", 10.0, "alpha", 10, 1, 100),
        ("p1", "p1-new", 20.0, "beta", 20, 2, 200),
        ("p2", "p2-new", 30.0, "gamma", 30, 3, 600),
    ):
        assert store.record_usage(
            **base,
            participant_id=participant_id,
            usage_key=key,
            ts=timestamp,
            model=model,
            harness="codex" if participant_id == "p1" else "claude",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microcents=cost,
        )

    result = store.usage_by_participant()
    assert result["truncated"] is False
    all_rows = result["participants"]
    assert [row["participant_id"] for row in all_rows] == ["p2", "p1"]
    assert all_rows[1] == {
        "participant_id": "p1",
        "harness": "codex",
        "models": ["alpha", "beta"],
        "input_tokens": 30,
        "output_tokens": 3,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "reasoning_output_tokens": 0,
        "cost_microcents": 300,
        "first_at": 10.0,
        "last_at": 20.0,
    }
    filtered = store.usage_by_participant(since=15.0, participant_ids=["p1"])
    assert filtered["participants"][0]["input_tokens"] == 20
    assert (
        store.usage_by_participant(participant_ids=["p2"])["participants"][0]["cost_microcents"]
        == 600
    )
    assert store.usage_by_participant(participant_ids=[]) == {
        "participants": [],
        "truncated": False,
    }
    limited = store.usage_by_participant(limit=1)
    assert limited["truncated"] is True
    assert [row["participant_id"] for row in limited["participants"]] == ["p2"]


def test_detailed_usage_by_harness_keeps_used_models_and_global_active_days(store):
    day_since = datetime(2026, 9, 1).timestamp()
    week_since = datetime(2026, 8, 31).timestamp()
    base = {
        "participant_id": "p1",
        "tree_root_id": "p1",
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "reasoning_output_tokens": 0,
        "output_tokens": 0,
    }
    for key, harness, model, timestamp, input_tokens, cost in (
        ("alpha-old", "codex", "alpha", datetime(2026, 8, 31, 12), 10, 100),
        ("alpha-today", "codex", "alpha", datetime(2026, 9, 1, 10), 2, 20),
        ("beta-today", "codex", "beta", datetime(2026, 9, 1, 11), 3, 30),
        ("unknown-model", "vibe", None, datetime(2026, 9, 1, 12), 4, 40),
        ("unused", "codex", "unused", datetime(2026, 8, 30, 12), 99, 990),
    ):
        assert store.record_usage(
            **base,
            usage_key=key,
            harness=harness,
            model=model,
            ts=timestamp.timestamp(),
            input_tokens=input_tokens,
            cost_microcents=cost,
        )

    result = store.usage_by_harness_detailed(
        day_since=day_since,
        week_since=week_since,
        month_since=day_since,
    )
    by_harness = {row["harness"]: row for row in result["harnesses"]}
    codex_models = {row["model"]: row for row in by_harness["codex"]["models"]}

    assert set(codex_models) == {"alpha", "beta"}
    assert codex_models["alpha"]["week"]["input_tokens"] == 12
    assert codex_models["beta"]["today"]["cost_microcents"] == 30
    assert by_harness["vibe"]["models"][0]["model"] is None
    assert by_harness["codex"]["week"]["input_tokens"] == 15
    assert result["totals"]["today"] == {
        "input_tokens": 9,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "reasoning_output_tokens": 0,
        "cost_microcents": 90,
        "active_days": 1,
    }
    assert result["totals"]["week"]["cost_microcents"] == 190
    assert result["totals"]["week"]["active_days"] == 2


def test_summary_cache_tracks_boundaries_inserts_and_timezone(store, monkeypatch):
    values = {
        "participant_id": "test",
        "tree_root_id": None,
        "model": "model",
        "harness": "vibe",
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "reasoning_output_tokens": 0,
        "cost_microcents": 100,
    }
    for ts in (10.0, 20.0, 30.0):
        store.record_usage(**values, usage_key=str(ts), ts=ts)
    repository = store._usage
    with patch.object(repository, "_read_summary", wraps=repository._read_summary) as reads:
        result = store.usage_summary(since=25.0, average_since=15.0)
        result["all_time"]["input_tokens"] = -1
        result = store.usage_summary(since=30.0, average_since=20.0)
        assert result["all_time"]["input_tokens"] == 3
        assert result["windowed"]["input_tokens"] == 1
        assert result["average"]["input_tokens"] == 2
        assert reads.call_count == 1
        result = store.usage_summary(since=30.1, average_since=20.1)
        assert result["windowed"]["input_tokens"] == 0
        assert result["average"]["input_tokens"] == 1
        assert reads.call_count == 2
        assert not store.record_usage(**values, usage_key="30.0", ts=30.0)
        store.usage_summary(since=31.0, average_since=21.0)
        assert reads.call_count == 2
        assert store.record_usage(**values, usage_key="backfilled", ts=25.0)
        assert store.record_usage(**values, usage_key="later", ts=86400.0 * 3)
        incremental = store.usage_summary(since=31.0, average_since=21.0)
        assert incremental["average"]["input_tokens"] == 3
        assert reads.call_count == 2  # inserts update the cached totals instead of rescanning
        truth = type(repository)._read_summary(repository, since=31.0, average_since=21.0)
        assert incremental == truth
        assert incremental["average"]["active_days"] == 2
        with monkeypatch.context() as timezone:
            timezone.setattr(usage_cache, "timezone_key", lambda: ("changed",))
            store.usage_summary(since=31.0, average_since=21.0)
        assert reads.call_count == 3
        assert store.usage_summary(since=10.0, average_since=10.0)["windowed"]["input_tokens"] == 5
        store.usage_summary(since=float("nan"), average_since=10.0)
        assert store.usage_summary(since=10.0, average_since=10.0)["windowed"]["input_tokens"] == 5


def test_a_timestamp_sqlite_cannot_date_drops_the_cache_instead_of_diverging(store):
    values = {
        "participant_id": "test",
        "tree_root_id": None,
        "model": "model",
        "harness": "vibe",
        "input_tokens": 1,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "reasoning_output_tokens": 0,
        "cost_microcents": 0,
    }
    store.record_usage(**values, usage_key="a", ts=10.0)
    store.usage_summary(since=5.0, average_since=5.0)
    cache = store._usage._summary_cache
    assert cache is not None
    columns = dict.fromkeys(cache.values["all_time"], 1)
    assert cache.with_row(10.0, columns) is not None
    for odd in (2362444531.0, -1615940787.0, float("nan"), 1.7e18):  # 2044, 1918, NaN, ns epoch
        assert cache.with_row(odd, columns) is None
    truth = type(store._usage)._read_summary
    # Nanosecond, year-2044 and 1918 rows: Python and SQLite can disagree on their local day.
    for index, ts in enumerate((1.7e18, 2362444531.0, -1615940787.0)):
        assert store.record_usage(**values, usage_key=f"odd{index}", ts=ts)
        summary = store.usage_summary(since=5.0, average_since=-1e12)
        assert summary == truth(store._usage, since=5.0, average_since=-1e12)
