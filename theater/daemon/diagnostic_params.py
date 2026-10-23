"""Shared parameter parsing for private and plugin diagnostic reads."""

from __future__ import annotations

from theater.constants import SECONDS_PER_DAY, USAGE_AVERAGE_WINDOW_DAYS
from theater.daemon import diagnostics
from theater.daemon.request_values import finite_number_param, integer_param
from theater.models import BadRequest, now


def window_since(params: dict, method_name: str) -> float | None:
    hours = (
        None
        if "window" not in params
        else finite_number_param(params["window"], "window", method_name=method_name)
    )
    return None if hours is None else now() - hours * 3600.0


def summary_args(params: dict) -> tuple[float, float, str | None]:
    hours = (
        24.0
        if "window" not in params
        else finite_number_param(params["window"], "window", method_name="usage_summary")
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
    limit = integer_param(params.get("limit", 100), "limit", method_name="bus.tail")
    after_id = integer_param(params.get("after_id", 0), "after_id", method_name="bus.tail")
    return limit, after_id
