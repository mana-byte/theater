"""Usage, stats, bus tail, and retention-floor RPC handlers."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select

from theater.constants import SECONDS_PER_DAY, USAGE_AVERAGE_WINDOW_DAYS
from theater.daemon.rpc.params import _finite_number_param, _integer_param
from theater.daemon.rpc.router import method
from theater.daemon.schema import bus, jobs
from theater.harness import describe
from theater.models import BadRequest, now


@method("bus.tail")
async def _bus_tail(daemon, params: dict) -> list[dict]:
    return daemon.store.bus_tail(
        limit=_integer_param(params.get("limit", 100), "limit", method_name="bus.tail"),
        after_id=_integer_param(params.get("after_id", 0), "after_id", method_name="bus.tail"),
    )


def _retention_floor(daemon) -> dict:
    """The oldest data actually present, per source.

    Returns {"jobs_from": float | None, "bus_from": float | None} — the
    earliest timestamp each table still holds, or None when the table is
    empty. Two floors rather than one because the two are backed by
    different tables under different retention: jobs outlive bus events by
    a wide margin, so a single number would misdescribe one of them.
    """
    jobs_floor = daemon.store.conn.execute(select(func.min(jobs.c.created_at))).scalar()
    bus_floor = daemon.store.conn.execute(select(func.min(bus.c.ts))).scalar()
    return {"jobs_from": jobs_floor, "bus_from": bus_floor}


@method("stats")
async def _stats(daemon, params: dict) -> dict:
    """How turns have been ending, per harness.

    Read straight out of SQLite on each call rather than kept as live counters:
    the numbers are only interesting over hours, a restart must not reset them,
    and a counter that exists solely to be printed is a thing to keep in sync
    for nothing.

    `window` is in hours and cuts on job creation time; omit it for all of
    history. Cutting on creation rather than completion so a turn that is still
    running counts in the window it was asked in.
    """
    window = params.get("window")
    hours = (
        None
        if "window" not in params
        else _finite_number_param(window, "window", method_name="stats")
    )
    since = None if hours is None else now() - hours * 3600.0
    return {
        "since": since,
        "coverage": _retention_floor(daemon),
        "harnesses": daemon.store.turn_outcomes(since=since),
        "refusals": daemon.store.refusal_counts(since=since),
    }


@method("usage_totals")
async def _usage_totals(daemon, params: dict) -> dict:
    """Aggregate token and cost totals across all participants."""
    window = params.get("window")
    hours = (
        None
        if "window" not in params
        else _finite_number_param(window, "window", method_name="usage_totals")
    )
    since = None if hours is None else now() - hours * 3600.0
    return {
        "since": since,
        **daemon.store.usage_totals(since=since),
    }


@method("usage_summary")
async def _usage_summary(daemon, params: dict) -> dict:
    """Aggregate footer usage windows in one synchronous SQLite scan."""
    window = params.get("window")
    hours = (
        24.0
        if "window" not in params
        else _finite_number_param(window, "window", method_name="usage_summary")
    )
    timestamp = now()
    if "period" not in params:
        requested_period = None
    else:
        requested_period = params["period"]
        if not isinstance(requested_period, str):
            raise BadRequest("usage_summary parameter 'period' must be a string")
    since = _calendar_period_since(requested_period, timestamp)
    resolved_period = requested_period if since is not None else None
    if since is None:
        # Compatibility for older clients and for unrecognised future period names.
        since = timestamp - hours * 3600.0
    average_since = timestamp - USAGE_AVERAGE_WINDOW_DAYS * SECONDS_PER_DAY
    return {
        "since": since,
        "average_since": average_since,
        "period": resolved_period,
        # `all_time` from this summary remains for older régies; current ones use `windowed`.
        **daemon.store.usage_summary(since=since, average_since=average_since),
    }


@method("usage_by_harness")
async def _usage_by_harness(daemon, params: dict) -> dict:
    """Usage for each loaded or historically observed harness over three periods."""
    timestamp = now()
    day_since = _calendar_period_since("day", timestamp)
    week_since = _calendar_period_since("week", timestamp)
    month_since = _calendar_period_since("month", timestamp)
    assert day_since is not None and week_since is not None and month_since is not None
    boundaries = {"day": day_since, "week": week_since, "month": month_since}
    detailed = params.get("detailed") is True
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
    result = {
        "since": boundaries,
        "harnesses": rows,
    }
    if detailed:
        result["totals"] = aggregated["totals"]
    return result


def _calendar_period_since(period: object, timestamp: float) -> float | None:
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
