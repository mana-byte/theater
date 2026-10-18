"""Summary reuse preserves exact moving windows, write visibility, and caller isolation."""

from __future__ import annotations

from unittest.mock import patch

from theater.daemon.persistence.repositories import usage_cache


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
