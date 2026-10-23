"""Daemon-owned usage, outcome, bus, and retention reads."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select

from theater.daemon.schema import bus, jobs
from theater.harness import describe


def bus_tail(daemon, *, limit: int, after_id: int) -> list[dict]:
    return daemon.store.bus_tail(limit=limit, after_id=after_id)


def retention_floor(daemon) -> dict:
    """The oldest data actually present, per source (``None`` when empty).

    Two floors because jobs outlive bus events by a wide margin under separate retention.
    """
    jobs_floor = daemon.store.conn.execute(select(func.min(jobs.c.created_at))).scalar()
    bus_floor = daemon.store.conn.execute(select(func.min(bus.c.ts))).scalar()
    return {"jobs_from": jobs_floor, "bus_from": bus_floor}


def stats(daemon, *, since: float | None) -> dict:
    """How turns have been ending, per harness.

    Read from SQLite each call so restarts never reset it. ``window`` (hours) cuts on
    creation time so a still-running turn counts in the window it was asked in.
    """
    return {
        "since": since,
        "coverage": retention_floor(daemon),
        "harnesses": daemon.store.turn_outcomes(since=since),
        "refusals": daemon.store.refusal_counts(since=since),
    }


def usage_totals(daemon, *, since: float | None) -> dict:
    """Aggregate token and cost totals across all participants."""
    return {
        "since": since,
        **daemon.store.usage_totals(since=since),
    }


def usage_summary(
    daemon, *, since: float, reported_since: float | None, average_since: float, period: str | None
) -> dict:
    """Aggregate footer usage windows in one synchronous SQLite scan."""
    return {
        "since": reported_since,
        "average_since": average_since,
        "period": period,
        # `all_time` from this summary remains for older régies; current ones use `windowed`.
        **daemon.store.usage_summary(since=since, average_since=average_since),
    }


def _usage_rows(observed: list[dict], *, detailed: bool) -> list[dict]:
    """Order loaded and historical harnesses, filling unloaded periods."""
    observed_by_name = {row["harness"]: row for row in observed}
    empty_period = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "reasoning_output_tokens": 0,
        "cost_microcents": 0,
        "active_days": 0,
    }
    # `describe` also reports plugins that failed to import.
    loaded = [row["name"] for row in describe() if not row["error"]]
    extra = sorted(set(observed_by_name) - set(loaded) - {"unknown"})
    names = [*loaded, *extra]
    unknown = observed_by_name.get("unknown")
    if unknown is not None and any(
        period["active_days"] > 0
        for period in (unknown["today"], unknown["week"], unknown["month"])
    ):
        names.append("unknown")

    rows = []
    for name in names:
        row = observed_by_name.get(name)
        if row is None:
            row = {
                "harness": name,
                "today": dict(empty_period),
                "week": dict(empty_period),
                "month": dict(empty_period),
            }
            if detailed:
                row["models"] = []
        elif detailed:
            row = {**row, "models": list(row.get("models", []))}
        rows.append(row)
    return rows


def usage_by_harness(daemon, *, timestamp: float, detailed: bool = False) -> dict:
    """Usage for each loaded or historically observed harness over three periods."""
    day_since = calendar_period_since("day", timestamp)
    week_since = calendar_period_since("week", timestamp)
    month_since = calendar_period_since("month", timestamp)
    assert day_since is not None and week_since is not None and month_since is not None
    boundaries = {"day": day_since, "week": week_since, "month": month_since}
    if detailed:
        aggregated = daemon.store.usage_by_harness_detailed(
            day_since=day_since,
            week_since=week_since,
            month_since=month_since,
        )
        observed = aggregated["harnesses"]
    else:
        aggregated = None
        observed = daemon.store.usage_by_harness(
            day_since=day_since,
            week_since=week_since,
            month_since=month_since,
        )
    result = {
        "since": boundaries,
        "harnesses": _usage_rows(observed, detailed=detailed),
    }
    if detailed:
        result["totals"] = aggregated["totals"]
    return result


def usage_by_harness_since(daemon, since: float, *, timestamp: float) -> dict[str, object]:
    boundaries: dict[str, float] = {}
    for period in ("day", "week", "month"):
        start = calendar_period_since(period, timestamp)
        assert start is not None
        boundaries[period] = max(start, since)
    observed = daemon.store.usage_by_harness(
        day_since=boundaries["day"],
        week_since=boundaries["week"],
        month_since=boundaries["month"],
    )
    return {"since": boundaries, "harnesses": _usage_rows(observed, detailed=False)}


def calendar_period_since(period: object, timestamp: float) -> float | None:
    """Return the local calendar boundary for a recognised usage period."""
    today = datetime.fromtimestamp(timestamp).date()
    if period == "day":
        boundary = today
    elif period == "week":
        # Theater uses the ISO convention: weeks begin on Monday.
        boundary = today - timedelta(days=today.weekday())
    elif period == "month":
        boundary = date(today.year, today.month, 1)
    elif period == "year":
        boundary = date(today.year, 1, 1)
    else:
        return None

    # Localise the boundary. Replacing fields on an aware `now` keeps the offset, wrong on DST days.
    return datetime.combine(boundary, time.min).astimezone().timestamp()
