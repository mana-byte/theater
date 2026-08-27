"""Bounded in-memory native OTel delivery queues."""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import cast

from theater.constants.harness import HARNESS_DEDUPE_MAX_FACTS, HARNESS_OTEL_DEDUPE_MAX_DELIVERIES
from theater.harness.channels.health import ChannelHealthTracker
from theater.harness.contracts.channels import ChannelDeclaration, OtelRecord, OtelSignal
from theater.harness.contracts.trajectory import TrajectoryFact


@dataclass(frozen=True, slots=True)
class OtelDelivery:
    """One accepted bounded native OTel record."""

    signal: OtelSignal
    binding_name: str
    record: OtelRecord
    native_id: str
    delivery_id: str


@dataclass(frozen=True, slots=True)
class OtelEnqueueResult:
    """Overflow is acknowledged so an exporter never waits on Theater."""

    accepted: bool
    duplicate: bool = False
    dropped: bool = False


@dataclass(slots=True)
class _InboxState:
    deliveries: deque[OtelDelivery]
    deliveries_seen: OrderedDict[str, None]
    facts_seen: OrderedDict[tuple[str, int], None]
    health: ChannelHealthTracker
    max_queue: int


class OtelInbox:
    """One nonblocking queue per participant and native OTel channel."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], _InboxState] = {}

    def register(self, participant_id: str, declaration: ChannelDeclaration) -> None:
        key = (participant_id, declaration.id)
        if key in self._states:
            return
        self._states[key] = _InboxState(
            deliveries=deque(),
            deliveries_seen=OrderedDict(),
            facts_seen=OrderedDict(),
            health=ChannelHealthTracker(declaration.id),
            max_queue=declaration.bounds.max_queue,
        )
        self._states[key].health.mark_starting()

    def enqueue(
        self,
        participant_id: str,
        declaration: ChannelDeclaration,
        delivery: OtelDelivery,
    ) -> OtelEnqueueResult:
        self.register(participant_id, declaration)
        state = self._states[(participant_id, declaration.id)]
        if delivery.delivery_id in state.deliveries_seen:
            return OtelEnqueueResult(accepted=True, duplicate=True)
        self._remember_delivery(state, delivery.delivery_id)
        if len(state.deliveries) >= state.max_queue:
            state.health.drop()
            state.health.mark_degraded("native OTel inbox overflow")
            return OtelEnqueueResult(accepted=True, dropped=True)
        state.deliveries.append(delivery)
        state.health.mark_healthy()
        return OtelEnqueueResult(accepted=True)

    @staticmethod
    def _remember_delivery(state: _InboxState, delivery_id: str) -> None:
        state.deliveries_seen[delivery_id] = None
        if len(state.deliveries_seen) > HARNESS_OTEL_DEDUPE_MAX_DELIVERIES:
            state.deliveries_seen.popitem(last=False)

    def delivery_seen(self, participant_id: str, channel_id: str, delivery_id: str) -> bool:
        state = self._states.get((participant_id, channel_id))
        return state is not None and delivery_id in state.deliveries_seen

    def drain(self, participant_id: str, channel_id: str) -> tuple[OtelDelivery, ...]:
        state = self._states.get((participant_id, channel_id))
        if state is None or not state.deliveries:
            return ()
        deliveries = tuple(state.deliveries)
        state.deliveries.clear()
        return deliveries

    def requeue(
        self,
        participant_id: str,
        channel_id: str,
        deliveries: tuple[OtelDelivery, ...],
    ) -> None:
        state = self._states.get((participant_id, channel_id))
        if state is None or not deliveries:
            return
        queued = deque(deliveries)
        queued.extend(state.deliveries)
        dropped = max(0, len(queued) - state.max_queue)
        while len(queued) > state.max_queue:
            queued.pop()
        state.deliveries = queued
        if dropped:
            state.health.drop(dropped)
            state.health.mark_degraded("native OTel inbox overflow")

    def accept_facts(
        self,
        participant_id: str,
        channel_id: str,
        facts: tuple[TrajectoryFact, ...],
    ) -> tuple[TrajectoryFact, ...]:
        state = self._states.get((participant_id, channel_id))
        if state is None:
            return ()
        accepted: list[TrajectoryFact] = []
        for fact in facts:
            key = (cast(str, fact.native_id), fact.revision)
            if key in state.facts_seen:
                continue
            state.facts_seen[key] = None
            if len(state.facts_seen) > HARNESS_DEDUPE_MAX_FACTS:
                state.facts_seen.popitem(last=False)
            accepted.append(fact)
        return tuple(accepted)

    def health(self, participant_id: str, channel_id: str) -> ChannelHealthTracker | None:
        state = self._states.get((participant_id, channel_id))
        return state.health if state is not None else None

    def drop_participant(self, participant_id: str) -> None:
        for key in tuple(self._states):
            if key[0] == participant_id:
                self._states.pop(key, None)

    async def aclose(self) -> None:
        self._states.clear()


__all__ = ["OtelDelivery", "OtelEnqueueResult", "OtelInbox"]
