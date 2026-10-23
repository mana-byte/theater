"""Request parsing and RPC adapters for daemon-owned diagnostic reads."""

from __future__ import annotations

from theater.constants import SECONDS_PER_DAY, USAGE_AVERAGE_WINDOW_DAYS
from theater.daemon import diagnostics
from theater.daemon.rpc.params import _finite_number_param, _integer_param
from theater.daemon.rpc.router import method
from theater.harness import describe
from theater.models import BadRequest, now

# Existing internal consumers still import these through the methods facade.
_retention_floor = diagnostics.retention_floor
_calendar_period_since = diagnostics.calendar_period_since


def window_since(params: dict, method_name: str) -> float | None:
    hours = (
        None
        if "window" not in params
        else _finite_number_param(params["window"], "window", method_name=method_name)
    )
    return None if hours is None else now() - hours * 3600.0


def summary_args(params: dict) -> tuple[float, float, str | None]:
    hours = (
        24.0
        if "window" not in params
        else _finite_number_param(params["window"], "window", method_name="usage_summary")
    )
    timestamp = now()
    if "period" not in params:
        requested_period = None
    else:
        requested_period = params["period"]
        if not isinstance(requested_period, str):
            raise BadRequest("usage_summary parameter 'period' must be a string")
    since = diagnostics.calendar_period_since(requested_period, timestamp)
    resolved_period = requested_period if since is not None else None
    if since is None:
        since = timestamp - hours * 3600.0
    average_since = timestamp - USAGE_AVERAGE_WINDOW_DAYS * SECONDS_PER_DAY
    return since, average_since, resolved_period


def bus_tail_args(params: dict) -> tuple[int, int]:
    limit = _integer_param(params.get("limit", 100), "limit", method_name="bus.tail")
    after_id = _integer_param(params.get("after_id", 0), "after_id", method_name="bus.tail")
    return limit, after_id


@method("bus.tail")
async def _bus_tail(daemon, params: dict) -> list[dict]:
    limit, after_id = bus_tail_args(params)
    return diagnostics.bus_tail(daemon, limit=limit, after_id=after_id)


@method("stats")
async def _stats(daemon, params: dict) -> dict:
    return diagnostics.stats(daemon, since=window_since(params, "stats"))


@method("usage_totals")
async def _usage_totals(daemon, params: dict) -> dict:
    return diagnostics.usage_totals(daemon, since=window_since(params, "usage_totals"))


@method("usage_summary")
async def _usage_summary(daemon, params: dict) -> dict:
    since, average_since, period = summary_args(params)
    return diagnostics.usage_summary(
        daemon, since=since, reported_since=since, average_since=average_since, period=period
    )


@method("usage_by_harness")
async def _usage_by_harness(daemon, params: dict) -> dict:
    return diagnostics.usage_by_harness(
        daemon, timestamp=now(), detailed=params.get("detailed") is True, describe_loaded=describe
    )
