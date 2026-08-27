"""Bounded in-memory hook delivery queues."""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from theater.constants.harness import HARNESS_DEDUPE_MAX_FACTS, HARNESS_HOOK_DEDUPE_MAX_DELIVERIES
from theater.harness.channels.health import ChannelHealthTracker
from theater.harness.contracts.channels import ChannelDeclaration, ChannelHealth
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.harness.contracts.values import freeze_json_mapping


@dataclass(frozen=True, slots=True)
class HookDelivery:
    """One accepted opaque hook envelope."""

    event: str
    payload: Mapping[str, object]
    native_id: str
    delivery_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise TypeError("hook payload must be a mapping")
        object.__setattr__(
            self,
            "payload",
            freeze_json_mapping(self.payload),
        )
        if not isinstance(self.native_id, str) or not self.native_id.strip():
            raise TypeError("hook delivery native_id must be a non-blank string")


@dataclass(frozen=True, slots=True)
class HookEnqueueResult:
    """Overflow is terminally acknowledged to the native sender."""

    accepted: bool
    duplicate: bool = False
    dropped: bool = False


@dataclass(slots=True)
class _InboxState:
    deliveries: deque[HookDelivery]
    deliveries_seen: OrderedDict[str, None]
    facts_seen: OrderedDict[tuple[str, int], None]
    health: ChannelHealthTracker
    max_queue: int


class HookInbox:
    """One nonblocking queue per participant and channel."""

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
        delivery: HookDelivery,
    ) -> HookEnqueueResult:
        self.register(participant_id, declaration)
        state = self._states[(participant_id, declaration.id)]
        if delivery.delivery_id is not None and self.delivery_seen(
            participant_id, declaration.id, delivery.delivery_id
        ):
            return HookEnqueueResult(accepted=True, duplicate=True)
        if delivery.delivery_id is not None:
            self._remember_delivery(state, delivery.delivery_id)
        if len(state.deliveries) >= declaration.bounds.max_queue:
            state.health.drop()
            state.health.mark_degraded("hook inbox overflow")
            return HookEnqueueResult(accepted=True, dropped=True)
        state.deliveries.append(delivery)
        state.health.record_accepted()
        state.health.mark_healthy()
        return HookEnqueueResult(accepted=True)

    @staticmethod
    def _remember_delivery(state: _InboxState, delivery_id: str) -> None:
        state.deliveries_seen[delivery_id] = None
        if len(state.deliveries_seen) > HARNESS_HOOK_DEDUPE_MAX_DELIVERIES:
            state.deliveries_seen.popitem(last=False)

    def delivery_seen(self, participant_id: str, channel_id: str, delivery_id: str) -> bool:
        state = self._states.get((participant_id, channel_id))
        return state is not None and delivery_id in state.deliveries_seen

    def drain(self, participant_id: str, channel_id: str) -> tuple[HookDelivery, ...]:
        state = self._states.get((participant_id, channel_id))
        if state is None or not state.deliveries:
            return ()
        deliveries = tuple(state.deliveries)
        state.deliveries.clear()
        return deliveries

    def requeue(
        self, participant_id: str, channel_id: str, deliveries: tuple[HookDelivery, ...]
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
            state.health.mark_degraded("hook inbox overflow")

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

    def health_snapshot(self, participant_id: str) -> tuple[ChannelHealth, ...]:
        return tuple(
            state.health.snapshot()
            for (current_id, _), state in sorted(self._states.items())
            if current_id == participant_id
        )

    def drop_participant(self, participant_id: str) -> None:
        for key in tuple(self._states):
            if key[0] == participant_id:
                self._states.pop(key, None)

    async def aclose(self) -> None:
        self._states.clear()


__all__ = ["HookDelivery", "HookEnqueueResult", "HookInbox"]
