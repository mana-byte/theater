"""Source adapter for bounded native OTel deliveries."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from functools import partial

from theater.harness.channels.otel.callbacks import (
    OtelCallbackBusy,
    OtelCallbackRunner,
    OtelCallbackTimeout,
)
from theater.harness.channels.otel.inbox import OtelInbox
from theater.harness.contracts.callbacks import OtelDecodeContext
from theater.harness.contracts.channels import ChannelFact, ChannelHealth, OtelBinding
from theater.harness.contracts.manifest import OtelChannelManifest
from theater.harness.contracts.source import Batch, Source
from theater.harness.contracts.trajectory import TrajectoryFact


def _decode_facts(
    context: OtelDecodeContext,
    *,
    binding: OtelBinding,
    limit: int,
) -> tuple[tuple[TrajectoryFact, ...], int]:
    value = binding.decoder(context)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("native OTel decoder must return a sequence of ChannelFact values")
    count = len(value)
    accepted: list[TrajectoryFact] = []
    for item_index in range(min(count, limit)):
        decoded = value[item_index]
        if not isinstance(decoded, ChannelFact):
            raise TypeError("native OTel decoder must return ChannelFact values")
        if decoded.signal not in binding.signals:
            raise ValueError("native OTel decoder emitted an undeclared signal")
        fact = decoded.fact
        if fact.native_id != context.native_id:
            raise ValueError("native OTel facts must use the accepted native_id")
        accepted.append(fact)
    return tuple(accepted), count - len(accepted)


class OtelSource(Source):
    """Decode accepted native OTel records into trajectory facts only."""

    def __init__(
        self,
        *,
        inbox: OtelInbox,
        callbacks: OtelCallbackRunner,
        participant_id: str,
        harness: str,
        channel: OtelChannelManifest,
    ) -> None:
        self._inbox = inbox
        self._callbacks = callbacks
        self._participant_id = participant_id
        self._harness = harness
        self._channel = channel
        self._bindings = {(binding.signal, binding.name): binding for binding in channel.bindings}
        self._closed = False

    async def read(self) -> Batch:  # noqa: PLR0912
        if self._closed:
            return Batch()
        facts: list[TrajectoryFact] = []
        failed = False
        remaining = self._channel.declaration.bounds.max_queue
        channel_id = self._channel.declaration.id
        tracker = self._inbox.health(self._participant_id, channel_id)
        deliveries = self._inbox.drain(self._participant_id, channel_id)
        for index, delivery in enumerate(deliveries):
            if remaining == 0:
                self._inbox.requeue(self._participant_id, channel_id, deliveries[index:])
                break
            binding = self._bindings.get((delivery.signal, delivery.binding_name))
            if binding is None:
                failed = True
                if tracker is not None:
                    tracker.drop()
                    tracker.mark_degraded("undeclared native OTel signal")
                continue
            context = OtelDecodeContext(
                participant_id=self._participant_id,
                harness=self._harness,
                channel_id=channel_id,
                record=delivery.record,
                delivery_id=delivery.delivery_id,
                native_id=delivery.native_id,
            )
            try:
                accepted, discarded = await self._callbacks.decode(
                    partial(_decode_facts, binding=binding, limit=remaining),
                    context,
                )
            except asyncio.CancelledError:
                self._inbox.requeue(self._participant_id, channel_id, deliveries[index:])
                raise
            except OtelCallbackBusy:
                failed = True
                self._inbox.requeue(self._participant_id, channel_id, deliveries[index:])
                if tracker is not None:
                    tracker.mark_degraded("native OTel decoder capacity exhausted")
                break
            except OtelCallbackTimeout:
                failed = True
                self._inbox.requeue(self._participant_id, channel_id, deliveries[index + 1 :])
                if tracker is not None:
                    tracker.drop()
                    tracker.mark_degraded("native OTel decoder timed out")
                break
            except Exception:
                failed = True
                if tracker is not None:
                    tracker.drop()
                    tracker.mark_degraded("native OTel decoder failed")
                continue
            if discarded and tracker is not None:
                tracker.drop(discarded)
                tracker.mark_degraded("native OTel output overflow")
            remaining -= len(accepted)
            facts.extend(self._inbox.accept_facts(self._participant_id, channel_id, accepted))
            if tracker is not None and not discarded:
                tracker.mark_healthy()
        return Batch(
            trajectory=tuple(facts),
            error_code="otel_decode_failed" if failed else None,
            error="native OTel decoder failed" if failed else None,
        )

    def channel_health(self) -> ChannelHealth | None:
        """Return the current bounded channel health snapshot."""
        tracker = self._inbox.health(self._participant_id, self._channel.declaration.id)
        return tracker.snapshot() if tracker is not None else None

    async def aclose(self) -> None:
        self._closed = True


__all__ = ["OtelSource"]
