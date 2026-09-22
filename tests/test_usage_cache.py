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
        assert store.usage_summary(since=31.0, average_since=21.0)["average"]["input_tokens"] == 2
        assert reads.call_count == 3
        with monkeypatch.context() as timezone:
            timezone.setattr(usage_cache, "timezone_key", lambda: ("changed",))
            store.usage_summary(since=31.0, average_since=21.0)
        assert reads.call_count == 4
        assert store.usage_summary(since=10.0, average_since=10.0)["windowed"]["input_tokens"] == 4
        store.usage_summary(since=float("nan"), average_since=10.0)
        assert store.usage_summary(since=10.0, average_since=10.0)["windowed"]["input_tokens"] == 4
