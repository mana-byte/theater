"""Retention-sweep helpers shared by the GC test modules."""

from __future__ import annotations

from sqlalchemy import func, select

from theater.config import RetentionSection


def retention(**overrides) -> RetentionSection:
    defaults = {
        "bus_days": 7,
        "events_days": 7,
        "jobs_days": 60,
        "refused_cap": 10000,
        "stale_running_days": 7,
        "batch": 5000,
        "interval": 3600.0,
        "enabled": True,
    }
    defaults.update(overrides)
    return RetentionSection(**defaults)


def count_rows(store, table) -> int:
    return store.conn.execute(select(func.count()).select_from(table)).scalar()
