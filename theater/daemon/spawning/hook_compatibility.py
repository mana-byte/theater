"""Read-only compatibility admission for optional launch-local hooks."""

from __future__ import annotations

import asyncio
import logging

from theater.daemon import workers
from theater.harness.contracts.manifest import HookChannelManifest
from theater.harness.contracts.runtime import RuntimeCompatibility, RuntimeProbeContext
from theater.models import Participant

logger = logging.getLogger("theater.spawner")

HOOK_PROBE_TIMEOUT_SECONDS = 10.0


async def probe_hook_channels(
    participant: Participant,
    harness,
    *,
    native_enabled: bool = True,
) -> frozenset[str]:
    """Select hooks without launching a CLI session or touching registry state.

    Existing hooks without a probe retain their established behavior. Optional
    native channels opt into probing and are omitted on failure or explicit
    legacy selection. Probe callbacks receive only frozen launch facts and run
    outside the daemon event loop; they must bound their own subprocesses too.
    """
    context = RuntimeProbeContext(
        participant_id=participant.id,
        binary=harness.binary,
        cwd=participant.cwd,
    )
    enabled: set[str] = set()
    for channel in harness.observer.enrichment_manifests():
        if (
            not isinstance(channel, HookChannelManifest)
            or channel.unavailable_reason is not None
            or not channel.bindings
            or channel.installer is None
        ):
            continue
        channel_id = channel.declaration.id
        if channel.probe is None:
            enabled.add(channel_id)
            continue
        if not native_enabled:
            continue
        try:
            async with asyncio.timeout(HOOK_PROBE_TIMEOUT_SECONDS):
                result = await workers.to_thread(
                    channel.probe,
                    context,
                    label="spawn.hook.compatibility",
                )
        except Exception as exc:
            logger.warning(
                "optional hook %s for %s omitted after %s; preserving ordinary launch",
                channel_id,
                participant.id,
                type(exc).__name__,
            )
            continue
        if not isinstance(result, RuntimeCompatibility):
            logger.warning(
                "optional hook %s for %s omitted after an invalid probe result",
                channel_id,
                participant.id,
            )
            continue
        if result.supported:
            enabled.add(channel_id)
        else:
            logger.info(
                "optional hook %s for %s omitted: %s",
                channel_id,
                participant.id,
                result.reason or "native compatibility was not established",
            )
    return frozenset(enabled)


__all__ = ["probe_hook_channels"]
