"""Public adapters for bounded daemon usage and diagnostic reads."""

from __future__ import annotations

import sys
from types import MappingProxyType

from theater.constants import SECONDS_PER_DAY, USAGE_AVERAGE_WINDOW_DAYS
from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.rpc.usage import _bus_tail, _calendar_period_since, _stats, _usage_by_harness
from theater.frontend.capabilities import METHOD_CATALOG
from theater.frontend.schemas import validator_for
from theater.harness import describe
from theater.models import now

_DEFAULT_SUMMARY_HOURS = 24.0


def _validated(method: str, result: dict[str, object]) -> dict[str, object]:
    validator_for(METHOD_CATALOG[method].result_schema_id).validate(result)
    return result


def _since(params: dict, *, default: float | None) -> float | None:
    value = params.get("since", default)
    if value is None:
        return None
    if type(value) not in {int, float}:
        raise TypeError("usage since must be a number or null")
    return float(value)


async def usage_totals(daemon, _context: ConnectionContext, params: dict) -> dict:
    since = _since(params, default=None)
    return _validated(
        "frontend.usage.totals",
        {"since": since, **daemon.store.usage_totals(since=since)},
    )


async def usage_summary(daemon, _context: ConnectionContext, params: dict) -> dict:
    timestamp = now()
    requested_since = _since(
        params, default=timestamp - _DEFAULT_SUMMARY_HOURS * SECONDS_PER_DAY / 24
    )
    summary_since = -sys.float_info.max if requested_since is None else requested_since
    average_since = timestamp - USAGE_AVERAGE_WINDOW_DAYS * SECONDS_PER_DAY
    return _validated(
        "frontend.usage.summary",
        {
            "since": requested_since,
            "average_since": average_since,
            "period": None,
            **daemon.store.usage_summary(since=summary_since, average_since=average_since),
        },
    )


def _usage_by_harness_since(daemon, since: float) -> dict[str, object]:
    timestamp = now()
    boundaries: dict[str, float] = {}
    for period in ("day", "week", "month"):
        start = _calendar_period_since(period, timestamp)
        assert start is not None
        boundaries[period] = max(start, since)
    observed = daemon.store.usage_by_harness(
        day_since=boundaries["day"],
        week_since=boundaries["week"],
        month_since=boundaries["month"],
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
    loaded = [row["name"] for row in describe() if not row["error"]]
    extra = sorted(set(observed_by_name) - set(loaded) - {"unknown"})
    names = [*loaded, *extra]
    unknown = observed_by_name.get("unknown")
    if unknown is not None and any(
        period["active_days"] > 0
        for period in (unknown["today"], unknown["week"], unknown["month"])
    ):
        names.append("unknown")
    rows = [
        observed_by_name.get(
            name,
            {
                "harness": name,
                "today": dict(empty_period),
                "week": dict(empty_period),
                "month": dict(empty_period),
            },
        )
        for name in names
    ]
    return {"since": boundaries, "harnesses": rows}


async def usage_by_harness(daemon, _context: ConnectionContext, params: dict) -> dict:
    since = _since(params, default=None)
    if params.get("detailed") is True:
        result = await _usage_by_harness(daemon, {"detailed": True})
    else:
        result = (
            await _usage_by_harness(daemon, {})
            if since is None
            else _usage_by_harness_since(daemon, since)
        )
    return _validated("frontend.usage.by_harness", result)


async def usage_by_participant(daemon, _context: ConnectionContext, params: dict) -> dict:
    since = _since(params, default=None)
    participant_ids = params.get("participant_ids")
    limit = params.get("limit", 500)
    result = {
        "since": since,
        **daemon.store.usage_by_participant(
            since=since,
            participant_ids=participant_ids,
            limit=limit,
        ),
    }
    return _validated("frontend.usage.by_participant", result)


async def stats_get(daemon, _context: ConnectionContext, _params: dict) -> dict:
    return _validated("frontend.stats.get", await _stats(daemon, {}))


async def bus_tail(daemon, _context: ConnectionContext, params: dict) -> dict:
    rows = await _bus_tail(daemon, params)
    after_id = params.get("after_id", 0)
    next_after_id = rows[-1]["id"] if rows else after_id
    return _validated(
        "frontend.bus.tail",
        {
            "items": rows,
            "next_cursor": None if not rows else str(next_after_id),
            "next_after_id": next_after_id,
        },
    )


DIAGNOSTIC_HANDLERS = MappingProxyType(
    {
        "frontend.usage.totals": usage_totals,
        "frontend.usage.summary": usage_summary,
        "frontend.usage.by_harness": usage_by_harness,
        "frontend.usage.by_participant": usage_by_participant,
        "frontend.stats.get": stats_get,
        "frontend.bus.tail": bus_tail,
    }
)

__all__ = ["DIAGNOSTIC_HANDLERS"]
