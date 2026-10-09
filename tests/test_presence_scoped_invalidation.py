"""Scoped presence invalidation: named terminals only, coalesced publication."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from theater.daemon.presence import PresenceMonitor, PresenceState
from theater.models import HumanPresent, Participant, TerminalBindingRecord

P1, T1 = "participant-a", "terminal-a"
P2, T2 = "participant-b", "terminal-b"
OCCUPANTS = {T1: "occupant-a", T2: "occupant-b"}
PARTICIPANTS = {T1: P1, T2: P2}


class Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def binding(terminal_id: str) -> TerminalBindingRecord:
    return TerminalBindingRecord(
        participant_id=PARTICIPANTS[terminal_id],
        provider_id="provider-a",
        provider_generation=7,
        terminal_id=terminal_id,
        terminal_incarnation=f"incarnation-{terminal_id}",
        occupant_evidence={"occupant_id": OCCUPANTS[terminal_id]},
        process_facts={"pid": 42, "started_at": 1.0, "executable": "/bin/agent"},
        health="healthy",
        report_revision=0,
        created_at=1.0,
        updated_at=1.0,
    )


def result(state: str, revision: int, terminal_id: str) -> dict:
    return {
        "provider_generation": 7,
        "report_revision": revision,
        "terminal": {
            "provider_id": "provider-a",
            "provider_generation": 7,
            "terminal_id": terminal_id,
            "terminal_incarnation": f"incarnation-{terminal_id}",
            "occupant": {"occupant_id": OCCUPANTS[terminal_id]},
            "process": {"pid": 42, "started_at": 1.0, "executable": "/bin/agent"},
        },
        "presence": {"state": state, "revision": revision, "reason": f"focus-{state}"},
        "screen": f"screen-{terminal_id}-{revision}",
    }


class Bindings:
    def __init__(self) -> None:
        self.values: dict[str, TerminalBindingRecord] = {P1: binding(T1), P2: binding(T2)}

    def get(self, participant_id: str) -> TerminalBindingRecord | None:
        return self.values.get(participant_id)


class Registry:
    def __init__(self) -> None:
        self.participants = [Participant(id=P1, harness="pi"), Participant(id=P2, harness="pi")]
        self.store = SimpleNamespace(terminal_bindings=Bindings())

    def get(self, participant_id: str) -> Participant | None:
        return next((item for item in self.participants if item.id == participant_id), None)

    def list(self, **_kwargs) -> list[Participant]:
        return self.participants


class Connections:
    generation = 7
    state = "online"

    def is_current(self, provider_id: str, generation: int) -> bool:
        return provider_id == "provider-a" and generation == self.generation

    def current_generation(self, provider_id: str) -> int | None:
        return self.generation if provider_id == "provider-a" else None

    def health(self, provider_id: str) -> str:
        assert provider_id == "provider-a"
        return self.state


class TerminalService:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self.connections = Connections()
        self.responses: dict[str, list] = {T1: [], T2: []}
        self.inspect_calls: dict[str, int] = {T1: 0, T2: 0}

    async def inspect(
        self,
        provider_id: str,
        generation: int,
        terminal_id: str,
        incarnation: str,
        *,
        screen_max_bytes: int = 0,
    ) -> dict:
        self.inspect_calls[terminal_id] += 1
        assert (provider_id, generation, incarnation) == (
            "provider-a",
            self.connections.generation,
            f"incarnation-{terminal_id}",
        )
        response = self.responses[terminal_id].pop(0)
        if isinstance(response, Exception):
            raise response
        participant_id = PARTICIPANTS[terminal_id]
        current = self.registry.store.terminal_bindings.values[participant_id]
        self.registry.store.terminal_bindings.values[participant_id] = replace(
            current,
            report_revision=response["report_revision"],
            provider_generation=generation,
            health="healthy",
        )
        return response


@pytest.fixture
async def scoped_monitor():
    registry = Registry()
    service = TerminalService(registry)
    monitor = PresenceMonitor(registry, clock=Clock(), stale_after=5.0, publish_window=0.05)
    monitor.configure_terminal_service(service)
    yield SimpleNamespace(monitor=monitor, service=service, registry=registry, clock=monitor._clock)
    await monitor.aclose()


async def _settle(monitor: PresenceMonitor, *, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while monitor._scoped_tasks or monitor._target_tasks:
        assert loop.time() < deadline, "presence refresh tasks did not settle"
        await asyncio.sleep(0.005)


async def _both_absent(scoped_monitor) -> None:
    service = scoped_monitor.service
    service.responses[T1].append(result("absent", 1, T1))
    service.responses[T2].append(result("absent", 1, T2))
    await scoped_monitor.monitor.refresh()
    assert scoped_monitor.monitor.snapshot(P1).state is PresenceState.ABSENT
    assert scoped_monitor.monitor.snapshot(P2).state is PresenceState.ABSENT


async def test_scoped_invalidation_rechecks_only_the_named_terminal(scoped_monitor) -> None:
    monitor, service = scoped_monitor.monitor, scoped_monitor.service
    await _both_absent(scoped_monitor)
    settled = monitor.revision

    service.responses[T1].append(result("absent", 2, T1))
    monitor.invalidate_provider("provider-a", 7, invalidated_terminals=[T1])
    assert monitor.revision > settled
    assert monitor.snapshot(P1).state is PresenceState.UNKNOWN
    assert monitor.snapshot(P2).state is PresenceState.ABSENT  # untouched terminal keeps evidence
    assert not monitor._wake.is_set()  # no global re-check scheduled

    await _settle(monitor)
    assert monitor.snapshot(P1).state is PresenceState.ABSENT
    assert service.inspect_calls == {T1: 2, T2: 1}  # the unnamed terminal is never re-checked


async def test_scoped_invalidation_fences_only_the_named_terminal(
    scoped_monitor, monkeypatch
) -> None:
    monitor, service = scoped_monitor.monitor, scoped_monitor.service
    await _both_absent(scoped_monitor)
    service.responses[T1].append(result("absent", 2, T1))
    service.responses[T2].append(result("absent", 2, T2))
    inspecting, release = asyncio.Event(), asyncio.Event()
    original = service.inspect

    async def inspect(provider_id, generation, terminal_id, incarnation, *, screen_max_bytes=0):
        if terminal_id == T2:
            inspecting.set()
            await release.wait()
        return await original(
            provider_id, generation, terminal_id, incarnation, screen_max_bytes=screen_max_bytes
        )

    monkeypatch.setattr(service, "inspect", inspect)
    pending = asyncio.create_task(monitor.refresh())
    try:
        await asyncio.wait_for(inspecting.wait(), 1)
        service.responses[T1].append(result("absent", 3, T1))
        monitor.invalidate_provider("provider-a", 7, invalidated_terminals=[T1])
        assert monitor.snapshot(P1).state is PresenceState.UNKNOWN
        assert monitor.snapshot(P2).state is PresenceState.ABSENT
        release.set()
        await pending
        await _settle(monitor)
        # The sibling's in-flight inspection predates the scoped invalidation and survives.
        assert monitor.snapshot(P2).reason == "focus-absent"
        assert monitor.snapshot(P1).state is PresenceState.ABSENT
        assert service.inspect_calls == {T1: 3, T2: 2}
    finally:
        release.set()
        await pending


async def test_empty_scope_rechecks_nothing(scoped_monitor) -> None:
    monitor = scoped_monitor.monitor
    await _both_absent(scoped_monitor)
    settled = monitor.revision

    monitor.invalidate_provider("provider-a", 7, invalidated_terminals=[])
    assert monitor.revision == settled
    assert not monitor._wake.is_set()
    assert monitor.snapshot(P1).state is PresenceState.ABSENT
    assert monitor.snapshot(P2).state is PresenceState.ABSENT
    await _settle(monitor)
    assert scoped_monitor.service.inspect_calls == {T1: 1, T2: 1}


async def test_missing_terminal_list_invalidates_all_for_backward_compatibility(
    scoped_monitor,
) -> None:
    monitor = scoped_monitor.monitor
    await _both_absent(scoped_monitor)

    monitor.invalidate_provider("provider-a", 7)
    assert monitor.snapshot(P1).state is PresenceState.UNKNOWN
    assert monitor.snapshot(P2).state is PresenceState.UNKNOWN
    assert monitor._wake.is_set()


async def test_stale_generation_scoped_invalidation_is_noop(scoped_monitor) -> None:
    monitor = scoped_monitor.monitor
    await _both_absent(scoped_monitor)
    settled = monitor.revision

    monitor.invalidate_provider("provider-a", 6, invalidated_terminals=[T1])
    assert monitor.revision == settled
    assert monitor.snapshot(P1).state is PresenceState.ABSENT
    assert not monitor._wake.is_set()


async def test_invalidation_safety_is_immediate_and_journal_coalesces_per_wave(
    scoped_monitor,
) -> None:
    monitor, service = scoped_monitor.monitor, scoped_monitor.service
    changes: list[str] = []
    monitor._on_change = changes.append
    await _both_absent(scoped_monitor)
    assert sorted(changes) == [P1, P2]  # refresh batches its publications before returning

    for revision in (2, 3):
        settled = monitor.revision
        service.responses[T1].append(result("absent", revision, T1))
        monitor.invalidate_provider("provider-a", 7, invalidated_terminals=[T1])
        # Safety is immediate in memory; only the journal callback waits in the window.
        assert monitor.revision > settled
        assert monitor.snapshot(P1).state is PresenceState.UNKNOWN
        await _settle(monitor)
        assert changes.count(P1) == revision - 1  # the wave is still coalesced
        await asyncio.sleep(0.08)
        assert changes.count(P1) == revision  # one batched publication per wave
        assert changes[-1] == P1  # the fired timer re-arms for the next wave


async def test_shutdown_drops_pending_publications_and_cancels_timers(scoped_monitor) -> None:
    monitor, service = scoped_monitor.monitor, scoped_monitor.service
    changes: list[str] = []
    monitor._on_change = changes.append
    await _both_absent(scoped_monitor)

    service.responses[T1].append(result("present", 2, T1))
    monitor.invalidate_provider("provider-a", 7, invalidated_terminals=[T1])
    await _settle(monitor)
    assert changes == [P1, P2]  # the whole wave still sits coalesced in the window

    await monitor.aclose()
    await asyncio.sleep(0.08)
    assert changes == [P1, P2]  # nothing is published after shutdown
    assert monitor._scoped_tasks == set()
    assert monitor._target_tasks == {}
    assert monitor._publisher._timer is None

    monitor.invalidate_provider("provider-a", 7, invalidated_terminals=[T1])
    assert changes == [P1, P2]


async def test_scoped_recheck_starts_fresh_when_named_inspection_is_in_flight(
    scoped_monitor, monkeypatch
) -> None:
    monitor, service = scoped_monitor.monitor, scoped_monitor.service
    await _both_absent(scoped_monitor)
    service.responses[T1].append(result("absent", 2, T1))
    inspecting, release = asyncio.Event(), asyncio.Event()
    original = service.inspect

    async def inspect(provider_id, generation, terminal_id, incarnation, *, screen_max_bytes=0):
        if terminal_id == T1:
            inspecting.set()
            await release.wait()
        return await original(
            provider_id, generation, terminal_id, incarnation, screen_max_bytes=screen_max_bytes
        )

    monkeypatch.setattr(service, "inspect", inspect)
    blocked = asyncio.create_task(monitor.require_absent(P1))
    try:
        await asyncio.wait_for(inspecting.wait(), 1)
        service.responses[T1].append(result("absent", 3, T1))
        monitor.invalidate_provider("provider-a", 7, invalidated_terminals=[T1])
        assert monitor.snapshot(P1).state is PresenceState.UNKNOWN

        release.set()
        # The pre-invalidation inspection is fenced, so admission stays protected.
        with pytest.raises(HumanPresent):
            await blocked

        await _settle(monitor)  # the scoped re-check must start a fresh inspection
        assert monitor.snapshot(P1).state is PresenceState.ABSENT
        assert monitor.snapshot(P1).reason == "focus-absent"
        assert monitor.snapshot(P2).state is PresenceState.ABSENT
        assert service.inspect_calls == {T1: 3, T2: 1}
    finally:
        release.set()
        with contextlib.suppress(BaseException):
            await blocked


async def test_report_scope_reaches_monitor_with_omitted_all_and_empty_none(scoped_monitor) -> None:
    from theater.daemon.terminals.service import TerminalProviderService

    monitor = scoped_monitor.monitor
    await _both_absent(scoped_monitor)
    parse = TerminalProviderService._invalidated_terminals

    monitor.invalidate_provider("provider-a", 7, invalidated_terminals=parse({"x": 1}))
    assert monitor._wake.is_set()  # omitted scope invalidates every terminal
    assert monitor.snapshot(P1).state is monitor.snapshot(P2).state is PresenceState.UNKNOWN


async def test_report_empty_scope_reaches_monitor_as_none(scoped_monitor) -> None:
    from theater.daemon.terminals.service import TerminalProviderService

    monitor = scoped_monitor.monitor
    await _both_absent(scoped_monitor)
    scope = TerminalProviderService._invalidated_terminals({"invalidated_terminals": []})
    monitor.invalidate_provider("provider-a", 7, invalidated_terminals=scope)
    assert monitor.snapshot(P1).state is monitor.snapshot(P2).state is PresenceState.ABSENT
