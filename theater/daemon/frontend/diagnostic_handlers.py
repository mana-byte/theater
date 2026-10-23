"""Public adapters for bounded daemon usage and diagnostic reads."""

from __future__ import annotations

import sys
from types import MappingProxyType

from theater.constants import SECONDS_PER_DAY, USAGE_AVERAGE_WINDOW_DAYS
from theater.daemon import diagnostics
from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.rpc.params import _finite_number_param, _integer_param
from theater.frontend.capabilities import METHOD_CATALOG
from theater.frontend.schemas import validator_for
from theater.models import BadRequest, now
from theater.protocol import BUS_TAIL_MAX_WAIT_SECONDS, BUS_TAIL_METHOD

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
        diagnostics.usage_totals(daemon, since=since),
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
        diagnostics.usage_summary(
            daemon,
            since=summary_since,
            reported_since=requested_since,
            average_since=average_since,
            period=None,
        ),
    )


async def usage_by_harness(daemon, _context: ConnectionContext, params: dict) -> dict:
    since = _since(params, default=None)
    if params.get("detailed") is True:
        result = diagnostics.usage_by_harness(daemon, timestamp=now(), detailed=True)
    else:
        result = (
            diagnostics.usage_by_harness(daemon, timestamp=now())
            if since is None
            else diagnostics.usage_by_harness_since(daemon, since, timestamp=now())
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
    return _validated("frontend.stats.get", diagnostics.stats(daemon, since=None))


async def bus_tail(daemon, _context: ConnectionContext, params: dict) -> dict:
    after_id = _integer_param(params.get("after_id", 0), "after_id", method_name="bus.tail")
    limit = _integer_param(params.get("limit", 100), "limit", method_name="bus.tail")
    wait = _finite_number_param(
        params.get("wait_seconds", 0), "wait_seconds", method_name="bus.tail"
    )
    kinds = params.get("kinds")
    if not 0 <= wait <= BUS_TAIL_MAX_WAIT_SECONDS:
        raise BadRequest(
            f"bus.tail wait_seconds must be between 0 and {BUS_TAIL_MAX_WAIT_SECONDS:g}"
        )
    if kinds is not None and not (isinstance(kinds, list) and all(type(k) is str for k in kinds)):
        raise BadRequest("bus.tail parameter 'kinds' must be a list of strings")
    tail = await daemon.bus_tail_waiter.tail(after_id, limit, kinds, wait)
    return _validated(
        BUS_TAIL_METHOD,
        {
            "items": tail.rows,
            "next_cursor": None if tail.next_after_id == after_id else str(tail.next_after_id),
            "next_after_id": tail.next_after_id,
        },
    )


DIAGNOSTIC_HANDLERS = MappingProxyType(
    {
        "frontend.usage.totals": usage_totals,
        "frontend.usage.summary": usage_summary,
        "frontend.usage.by_harness": usage_by_harness,
        "frontend.usage.by_participant": usage_by_participant,
        "frontend.stats.get": stats_get,
        BUS_TAIL_METHOD: bus_tail,
    }
)

__all__ = ["DIAGNOSTIC_HANDLERS"]
