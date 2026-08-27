"""Source adapter for bounded generic hook deliveries."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from functools import partial

from theater.harness.channels.hooks.callbacks import (
    HookCallbackBusy,
    HookCallbackRunner,
    HookCallbackTimeout,
)
from theater.harness.channels.hooks.inbox import HookInbox
from theater.harness.contracts.callbacks import HookDecodeContext
from theater.harness.contracts.channels import ChannelFact, ChannelHealth, HookBinding
from theater.harness.contracts.manifest import HookChannelManifest
from theater.harness.contracts.source import Batch, Source
from theater.harness.contracts.trajectory import TrajectoryFact


def _decode_facts(
    context: HookDecodeContext,
    *,
    binding: HookBinding,
    limit: int,
) -> tuple[tuple[TrajectoryFact, ...], int]:
    value = binding.decoder(context)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("hook decoder must return a sequence of ChannelFact values")
    count = len(value)
    accepted: list[TrajectoryFact] = []
    for item_index in range(min(count, limit)):
        decoded = value[item_index]
        if not isinstance(decoded, ChannelFact):
            raise TypeError("hook decoder must return ChannelFact values")
        if decoded.signal not in binding.signals:
            raise ValueError("hook decoder emitted an undeclared signal")
        fact = decoded.fact
        if fact.native_id != context.native_id:
            raise ValueError("hook facts must use the accepted native_id")
        accepted.append(fact)
    return tuple(accepted), count - len(accepted)


class HookSource(Source):
    """Decode declared hook events into trajectory facts only."""

    def __init__(
        self,
        *,
        inbox: HookInbox,
        callbacks: HookCallbackRunner,
        participant_id: str,
        channel: HookChannelManifest,
    ) -> None:
        self._inbox = inbox
        self._callbacks = callbacks
        self._participant_id = participant_id
        self._channel = channel
        self._bindings = {binding.event: binding for binding in channel.bindings}
        self._closed = False

    async def read(self) -> Batch:  # noqa: PLR0912
        if self._closed:
            return Batch()
        facts: list[TrajectoryFact] = []
        failed = False
        remaining = self._channel.declaration.bounds.max_queue
        tracker = self._inbox.health(self._participant_id, self._channel.declaration.id)
        deliveries = self._inbox.drain(self._participant_id, self._channel.declaration.id)
        for index, delivery in enumerate(deliveries):
            if remaining == 0:
                self._inbox.requeue(
                    self._participant_id,
                    self._channel.declaration.id,
                    deliveries[index:],
                )
                break
            binding = self._bindings.get(delivery.event)
            if binding is None:
                failed = True
                if tracker is not None:
                    tracker.drop()
                    tracker.mark_degraded("undeclared hook event")
                continue
            context = HookDecodeContext(
                participant_id=self._participant_id,
                channel_id=self._channel.declaration.id,
                event=delivery.event,
                payload=delivery.payload,
                delivery_id=delivery.delivery_id,
                native_id=delivery.native_id,
            )
            try:
                accepted, discarded = await self._callbacks.decode(
                    partial(_decode_facts, binding=binding, limit=remaining), context
                )
            except asyncio.CancelledError:
                self._inbox.requeue(
                    self._participant_id,
                    self._channel.declaration.id,
                    deliveries[index:],
                )
                raise
            except HookCallbackBusy:
                failed = True
                self._inbox.requeue(
                    self._participant_id,
                    self._channel.declaration.id,
                    deliveries[index:],
                )
                if tracker is not None:
                    tracker.mark_degraded("hook decoder capacity exhausted")
                break
            except HookCallbackTimeout:
                failed = True
                self._inbox.requeue(
                    self._participant_id,
                    self._channel.declaration.id,
                    deliveries[index + 1 :],
                )
                if tracker is not None:
                    tracker.drop()
                    tracker.mark_degraded("hook decoder timed out")
                break
            except Exception:
                failed = True
                if tracker is not None:
                    tracker.drop()
                    tracker.mark_degraded("hook decoder failed")
                continue
            if discarded and tracker is not None:
                tracker.drop(discarded)
                tracker.mark_degraded("hook output overflow")
            remaining -= len(accepted)
            facts.extend(
                self._inbox.accept_facts(
                    self._participant_id,
                    self._channel.declaration.id,
                    accepted,
                )
            )
            if tracker is not None and not discarded:
                tracker.mark_healthy()
        return Batch(
            trajectory=tuple(facts),
            error_code="hook_decode_failed" if failed else None,
            error="hook decoder failed" if failed else None,
        )

    def channel_health(self) -> ChannelHealth | None:
        """Return the current bounded channel health snapshot."""
        tracker = self._inbox.health(self._participant_id, self._channel.declaration.id)
        return tracker.snapshot() if tracker is not None else None

    def health_snapshot(self) -> tuple[ChannelHealth, ...]:
        health = self.channel_health()
        return () if health is None else (health,)

    async def aclose(self) -> None:
        self._closed = True


__all__ = ["HookSource"]
