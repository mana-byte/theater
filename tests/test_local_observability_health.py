"""Local historical health must not pass on a live-only or unreadable trace index."""

from __future__ import annotations

import pytest

from dev.observability import health


@pytest.mark.parametrize("case", ["healthy", "short_id", "no_history", "unreadable"])
def test_history_health_requires_search_and_trace_read(monkeypatch, case):
    trace_id = "a" * (29 if case == "short_id" else 32)
    healthy = case in {"healthy", "short_id"}

    def query(path, **params):
        if path == "api/search":
            assert params == {"q": "{}", "start": 71200, "end": 98200, "limit": 1}
            return {"traces": [] if case == "no_history" else [{"traceID": trace_id}]}
        assert path == f"api/traces/{trace_id.zfill(32)}"
        return {"batches": [{"scopeSpans": [{"spans": [{}]}]}]} if healthy else {}

    monkeypatch.setattr(health, "query", query)
    if healthy:
        assert health.inspect_history(now=100000) == trace_id.zfill(32)
    else:
        with pytest.raises(ValueError, match="Historical"):
            health.inspect_history(now=100000)
