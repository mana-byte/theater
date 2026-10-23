"""Daemon-owned harness and model discovery shared by wire adapters."""

from __future__ import annotations

import asyncio
import logging

from theater.harness import (
    HARNESSES,
    describe,
    native_compatibility_probe,
    native_compatibility_record,
    normalize,
    supports_model,
    supports_reasoning,
)
from theater.harness.channels.health import merge_channel_health
from theater.harness.contracts.channels import ChannelHealth
from theater.harness.contracts.runtime import RuntimeCompatibility, RuntimeProbeContext
from theater.harness.registry.lookup import ProbeOutcome

logger = logging.getLogger("theater.daemon.rpc.spawning")


async def harnesses(daemon) -> list[dict]:
    """Describe the daemon's loaded harnesses and live channel health."""
    runtime: dict[str, dict[str, tuple[ChannelHealth, ...]]] = {}
    for participant in daemon.registry.list():
        snapshot = daemon.observer.channel_health_snapshot(participant.id)
        supplemental = (
            *daemon.hook_runtime.health_snapshot(participant.id),
            *daemon.otel_runtime.health_snapshot(participant.id),
        )
        health_by_id: dict[str, ChannelHealth] = {item.channel_id: item for item in snapshot}
        for item in supplemental:
            current = health_by_id.get(item.channel_id)
            health_by_id[item.channel_id] = (
                item if current is None else merge_channel_health(current, item)
            )
        health = tuple(health_by_id.values())
        if health:
            runtime.setdefault(normalize(participant.harness), {})[participant.id] = health
    rows = describe(runtime=runtime)

    async def probe(row: dict) -> ProbeOutcome:
        """An exception records a failed probe; None means no probe."""
        harness = HARNESSES.get(row["name"])
        if harness is None or not row["installed"] or row["error"]:
            return None
        callback = native_compatibility_probe(harness)
        if callback is None:
            return None
        try:
            result = await daemon.compatibility_probes.probe(
                row["name"],
                callback,
                RuntimeProbeContext(binary=row["path"]),
                configuration=daemon.config,
            )
        except Exception as error:
            logger.warning(
                "compatibility probe for harness %s failed: %s", row["name"], error, exc_info=error
            )
            return error
        return result if isinstance(result, RuntimeCompatibility) else None

    results = await asyncio.gather(*(probe(row) for row in rows))
    for row, outcome in zip(rows, results, strict=True):
        harness = HARNESSES.get(row["name"])
        if harness is not None:
            row["native_compatibility"] = native_compatibility_record(
                harness,
                installed=bool(row["installed"]),
                result=outcome if isinstance(outcome, RuntimeCompatibility) else None,
                error=outcome if isinstance(outcome, Exception) else None,
            )
    return rows


def models(daemon) -> list[dict]:
    """Describe the model and reasoning policy held by this daemon."""
    rows = []
    for row in describe():
        name = row["name"]
        harness = HARNESSES.get(name)
        rows.append(
            {
                "harness": name,
                "models": daemon.config.models_for(name),
                "supported": harness is not None and supports_model(harness),
                "reasoning": daemon.config.reasoning_for(name),
                "reasoning_supported": harness is not None and supports_reasoning(harness),
                "installed": row["installed"],
                "error": row["error"],
            }
        )
    return rows
