"""Request parsing and RPC adapters for daemon-owned diagnostic reads."""

from __future__ import annotations

from theater.daemon import diagnostic_params, diagnostics
from theater.daemon.rpc.router import method
from theater.models import now


@method("bus.tail")
async def _bus_tail(daemon, params: dict) -> list[dict]:
    limit, after_id = diagnostic_params.bus_tail_args(params)
    return diagnostics.bus_tail(daemon, limit=limit, after_id=after_id)


@method("stats")
async def _stats(daemon, params: dict) -> dict:
    return diagnostics.stats(daemon, since=diagnostic_params.window_since(params, "stats"))


@method("usage_totals")
async def _usage_totals(daemon, params: dict) -> dict:
    return diagnostics.usage_totals(
        daemon, since=diagnostic_params.window_since(params, "usage_totals")
    )


@method("usage_summary")
async def _usage_summary(daemon, params: dict) -> dict:
    since, average_since, period = diagnostic_params.summary_args(params)
    return diagnostics.usage_summary(
        daemon, since=since, reported_since=since, average_since=average_since, period=period
    )


@method("usage_by_harness")
async def _usage_by_harness(daemon, params: dict) -> dict:
    return diagnostics.usage_by_harness(
        daemon, timestamp=now(), detailed=params.get("detailed") is True
    )
