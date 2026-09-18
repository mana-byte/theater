"""Provider-backed human-presence and lifecycle evidence fences."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from theater.daemon.observer import Observer
from theater.daemon.presence import PresenceMonitor, PresenceState
from theater.daemon.presence.lifecycle import retire_authoritative_exit
from theater.daemon.presence.provider import ProviderExitEvidence
from theater.harness import get as get_harness
from theater.models import HumanPresent, Participant, TerminalBindingRecord


class Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class Bindings:
    def __init__(self, binding: TerminalBindingRecord) -> None:
        self.value = binding

    def get(self, participant_id: str) -> TerminalBindingRecord | None:
        return self.value if self.value.participant_id == participant_id else None


class Registry:
    def __init__(self, participant: Participant, binding: TerminalBindingRecord) -> None:
        self.participant = participant
        self.store = SimpleNamespace(terminal_bindings=Bindings(binding))

    def get(self, participant_id: str) -> Participant | None:
        return self.participant if self.participant.id == participant_id else None

    def list(self, **_kwargs) -> list[Participant]:
        return [self.participant]


class Connections:
    def __init__(self) -> None:
        self.generation = 7
        self.state = "online"

    def is_current(self, provider_id: str, generation: int) -> bool:
        return provider_id == "provider-a" and generation == self.generation

    def health(self, provider_id: str) -> str:
        assert provider_id == "provider-a"
        return self.state


class TerminalService:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self.connections = Connections()
        self.responses: list[dict | Exception] = []

    async def inspect(
        self,
        provider_id: str,
        generation: int,
        terminal_id: str,
        incarnation: str,
    ) -> dict:
        assert (provider_id, generation, terminal_id, incarnation) == (
            "provider-a",
            7,
            "terminal-a",
            "incarnation-a",
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        revision = response["report_revision"]
        self.registry.store.terminal_bindings.value = replace(
            self.registry.store.terminal_bindings.value,
            report_revision=revision,
        )
        return response


def binding(*, health: str = "healthy") -> TerminalBindingRecord:
    return TerminalBindingRecord(
        participant_id="participant-a",
        provider_id="provider-a",
        provider_generation=7,
        terminal_id="terminal-a",
        terminal_incarnation="incarnation-a",
        occupant_evidence={"occupant_id": "occupant-a"},
        process_facts={"pid": 42, "started_at": 1.0, "executable": "/bin/agent"},
        health=health,
        report_revision=0,
        created_at=1.0,
        updated_at=1.0,
    )


def result(
    state: str,
    revision: int,
    *,
    occupant: str = "occupant-a",
    lifecycle: dict | None = None,
) -> dict:
    value = {
        "provider_generation": 7,
        "report_revision": revision,
        "terminal": {
            "provider_id": "provider-a",
            "provider_generation": 7,
            "terminal_id": "terminal-a",
            "terminal_incarnation": "incarnation-a",
            "occupant": {"occupant_id": occupant},
            "process": {"pid": 42, "started_at": 1.0, "executable": "/bin/agent"},
        },
        "presence": {"state": state, "revision": revision, "reason": f"focus-{state}"},
        "screen": f"screen-{revision}",
    }
    if lifecycle is not None:
        value["lifecycle"] = lifecycle
    return value


@pytest.fixture
def provider_monitor():
    clock = Clock()
    participant = Participant(id="participant-a", harness="pi")
    registry = Registry(participant, binding())
    service = TerminalService(registry)
    monitor = PresenceMonitor(registry, clock=clock, stale_after=5.0)
    monitor.configure_terminal_service(service)

    return monitor, service, registry, clock


@pytest.mark.parametrize("state", ["present", "absent", "unknown"])
async def test_provider_inspect_projects_public_presence_states(
    provider_monitor, state: str
) -> None:
    monitor, service, _registry, _clock = provider_monitor
    service.responses.append(result(state, 1))

    await monitor.refresh()

    snapshot = monitor.snapshot("participant-a")
    assert snapshot.state is PresenceState(state)
    assert snapshot.reason == f"focus-{state}"
    assert monitor.terminal_screen("participant-a") == "screen-1"


async def test_provider_loss_and_stale_generation_never_become_no_pane_absence(
    provider_monitor,
) -> None:
    monitor, service, registry, _clock = provider_monitor
    service.connections.state = "offline"
    await monitor.refresh()
    assert monitor.snapshot("participant-a").state is PresenceState.UNKNOWN
    assert monitor.snapshot("participant-a").reason == "provider-offline"
    assert monitor.terminal_screen("participant-a") is None
    with pytest.raises(HumanPresent):
        await monitor.require_absent("participant-a")

    service.connections.state = "online"
    service.connections.generation = 8
    await monitor.refresh()
    assert monitor.snapshot("participant-a").reason == "provider-generation-stale"

    registry.store.terminal_bindings.value = replace(binding(), health="missing")
    service.connections.generation = 7
    await monitor.refresh()
    assert monitor.snapshot("participant-a").reason == "terminal-missing"


async def test_no_binding_and_no_pane_preserves_non_terminal_absence(provider_monitor) -> None:
    monitor, _service, registry, _clock = provider_monitor
    registry.store.terminal_bindings.get = lambda participant_id: None
    assert monitor.snapshot("participant-a").state is PresenceState.ABSENT
    await monitor.require_absent("participant-a")


async def test_expired_provider_evidence_fails_closed(provider_monitor) -> None:
    monitor, service, _registry, clock = provider_monitor
    service.responses.append(result("absent", 1))
    await monitor.refresh()
    clock.value += 6
    snapshot = monitor.snapshot("participant-a")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "provider-evidence-stale"


async def test_refresh_rechecks_focus_immediately_before_each_delivery(provider_monitor) -> None:
    monitor, service, _registry, _clock = provider_monitor
    service.responses.extend([result("absent", 1), result("present", 2)])

    await monitor.require_absent("participant-a")
    with pytest.raises(HumanPresent):
        await monitor.require_absent("participant-a")


async def test_provider_presence_has_no_legacy_inventory_dependency(provider_monitor) -> None:
    monitor, service, _registry, _clock = provider_monitor
    service.responses.append(result("absent", 1))
    await monitor.require_absent("participant-a")
    assert monitor.snapshot("participant-a").state is PresenceState.ABSENT


async def test_inspect_failure_identity_mismatch_and_revision_regression_fail_closed(
    provider_monitor,
) -> None:
    monitor, service, registry, _clock = provider_monitor
    service.responses.append(result("absent", 2))
    await monitor.refresh()
    assert monitor.snapshot("participant-a").state is PresenceState.ABSENT

    service.responses.append(RuntimeError("callback lost"))
    await monitor.refresh()
    assert monitor.snapshot("participant-a").reason.startswith("provider-inspect-failed")

    service.responses.append(result("absent", 3, occupant="replacement"))
    await monitor.refresh()
    assert monitor.snapshot("participant-a").reason == "provider-evidence-mismatch"

    registry.store.terminal_bindings.value = replace(binding(), report_revision=3)
    service.responses.append(result("present", 1))
    await monitor.refresh()
    assert monitor.snapshot("participant-a").reason == "provider-presence-regressed"


async def test_provider_revision_wakes_waiters_without_lost_wakeup(provider_monitor) -> None:
    monitor, service, _registry, _clock = provider_monitor
    after = monitor.revision
    waiter = asyncio.create_task(monitor.wait_for_change(after))
    service.responses.append(result("absent", 1))
    await monitor.refresh()
    assert await asyncio.wait_for(waiter, 1) > after


async def test_only_exact_authoritative_exit_reaches_lifecycle_handler(
    provider_monitor,
) -> None:
    monitor, service, registry, _clock = provider_monitor
    exits = []

    async def retire(evidence) -> bool:
        exits.append(evidence)
        return True

    monitor.configure_terminal_service(service, exit_handler=retire)
    service.responses.append(result("unknown", 1, lifecycle={"alive": False}))
    await monitor.refresh()
    assert [e.participant_id for e in exits] == ["participant-a"]
    assert monitor.snapshot("participant-a").reason == "terminal-exited"

    service.responses.append(result("unknown", 2, lifecycle={}))
    await monitor.refresh()
    assert len(exits) == 1

    registry.store.terminal_bindings.value = replace(binding(), health="missing")
    await monitor.refresh()
    assert len(exits) == 1
    assert monitor.snapshot("participant-a").state is PresenceState.UNKNOWN


async def test_unsettled_authoritative_exit_remains_protected(provider_monitor) -> None:
    monitor, service, _registry, _clock = provider_monitor

    async def refuse_retirement(_evidence) -> bool:
        return False

    monitor.configure_terminal_service(service, exit_handler=refuse_retirement)
    service.responses.append(result("absent", 1, lifecycle={"alive": False}))
    await monitor.refresh()
    snapshot = monitor.snapshot("participant-a")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "terminal-exit-unsettled"


async def test_authoritative_exit_adapter_revalidates_before_retirement(monkeypatch) -> None:
    participant = Participant(id="participant-a", harness="pi")
    registry = Registry(participant, binding())
    registry.marked_dead = []
    registry.mark_dead = registry.marked_dead.append
    registry.store.get_participant = lambda participant_id: participant
    registry.store.running_jobs_for_target = lambda participant_id: []
    service = TerminalService(registry)
    released = []
    daemon = SimpleNamespace(
        store=registry.store,
        registry=registry,
        terminal_service=service,
        jobs=SimpleNamespace(finish=lambda *args, **kwargs: None),
        spawner=SimpleNamespace(
            release_workspace_usage=lambda *args, **kwargs: released.append(args)
        ),
    )

    async def teardown(_daemon, participant_id: str, *, caller_id: str) -> bool:
        assert (participant_id, caller_id) == ("participant-a", "cli")
        return True

    monkeypatch.setattr(
        "theater.daemon.runtime.recovery.teardown_participant_runtime",
        teardown,
    )
    evidence = ProviderExitEvidence(
        participant_id="participant-a",
        provider_id="provider-a",
        provider_generation=7,
        terminal_id="terminal-a",
        terminal_incarnation="incarnation-a",
        occupant_evidence={"occupant_id": "occupant-a"},
        process_facts={"pid": 42, "started_at": 1.0, "executable": "/bin/agent"},
        report_revision=1,
        presence_revision=1,
        lifecycle={"alive": False},
    )

    assert await retire_authoritative_exit(daemon, evidence) is True
    assert registry.marked_dead == ["participant-a"]
    assert released

    registry.marked_dead.clear()
    replaced = replace(evidence, terminal_incarnation="replacement")
    assert await retire_authoritative_exit(daemon, replaced) is False
    assert registry.marked_dead == []


async def test_provider_bound_paneless_participant_keeps_a_screen_observer(registry) -> None:
    participant = registry.create_spawned(harness="pi", cwd=None)
    with registry.store.write_unit() as unit:
        registry.store.terminal_bindings.bind(
            replace(binding(), participant_id=participant.id),
            connection=unit.connection,
        )
    observer = Observer(registry, {"pi": get_harness("pi")}, screen=0.01)
    observer.set_terminal_evidence_provider(
        SimpleNamespace(terminal_screen=lambda participant_id: "provider screen")
    )
    try:
        observer._start_watch(participant.id)
        assert participant.id in observer._tasks
        assert observer._provider_screen(participant.id) == "provider screen"
    finally:
        await observer.aclose()
