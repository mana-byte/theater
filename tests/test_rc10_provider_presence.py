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
from theater.models import (
    HumanPresent,
    JobState,
    Participant,
    ProviderRecord,
    Status,
    TerminalBindingRecord,
    now,
)


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

    def current_generation(self, provider_id: str) -> int | None:
        return self.generation if provider_id == "provider-a" else None

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
            self.connections.generation,
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
            provider_generation=generation,
            health="healthy",
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


async def test_focus_invalidation_protects_cache_and_fences_inflight_inspection(
    provider_monitor, monkeypatch
):
    monitor, service, _registry, _clock = provider_monitor
    service.responses.append(result("absent", 1))
    await monitor.refresh()
    revision = monitor.revision
    monitor.invalidate_provider("provider-a", 6)
    assert monitor.revision == revision
    assert monitor.snapshot("participant-a").state is PresenceState.ABSENT

    inspecting, release = asyncio.Event(), asyncio.Event()
    original = service.inspect

    async def inspect(*args):
        inspecting.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(service, "inspect", inspect)
    service.responses.append(result("absent", 2))
    pending = asyncio.create_task(monitor.refresh())
    try:
        await asyncio.wait_for(inspecting.wait(), 1)
        monitor.invalidate_provider("provider-a", 7)
        assert monitor.revision > revision
        assert monitor.snapshot("participant-a").state is PresenceState.UNKNOWN
        assert monitor._wake.is_set()
        release.set()
        await pending
        assert (
            monitor.snapshot("participant-a").reason == "provider-presence-changed-during-inspect"
        )
        service.responses.append(result("absent", 3))
        await monitor.require_absent("participant-a")
        assert monitor.snapshot("participant-a").state is PresenceState.ABSENT
    finally:
        release.set()
        await pending
        await monitor.aclose()


async def test_target_admission_does_not_wait_for_unrelated_background_inspection(
    provider_monitor, monkeypatch
):
    monitor, service, registry, _clock = provider_monitor
    sibling = Participant(id="unrelated", harness="pi")
    monkeypatch.setattr(registry, "list", lambda: [registry.participant, sibling])
    monkeypatch.setattr(
        registry, "get", lambda pid: registry.participant if pid == "participant-a" else sibling
    )
    inspecting_sibling = asyncio.Event()
    release_sibling = asyncio.Event()
    original = monitor._provider.refresh

    async def refresh(participants):
        if participants[0].id == sibling.id:
            inspecting_sibling.set()
            await release_sibling.wait()
            return True
        return await original(participants)

    monkeypatch.setattr(monitor._provider, "refresh", refresh)
    service.responses.extend([result("absent", 1), result("present", 2)])
    background = asyncio.create_task(monitor.refresh())
    try:
        await asyncio.wait_for(inspecting_sibling.wait(), 1)
        with pytest.raises(HumanPresent):
            await asyncio.wait_for(monitor.require_absent("participant-a"), 1)
        assert not background.done()
        assert not service.responses
    finally:
        release_sibling.set()
        await background
        await monitor.aclose()


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
    service.responses.append(RuntimeError("old terminal was not verified by the new generation"))
    await monitor.refresh()
    assert monitor.snapshot("participant-a").reason == "provider-generation-stale"

    registry.store.terminal_bindings.value = replace(binding(), health="missing")
    service.connections.generation = 7
    service.responses.append(RuntimeError("missing inventory is not terminal-exit evidence"))
    await monitor.refresh()
    assert monitor.snapshot("participant-a").reason == "terminal-missing"


async def test_no_terminal_evidence_is_unknown_even_with_a_healthy_native_route(
    provider_monitor,
) -> None:
    monitor, _service, registry, _clock = provider_monitor
    registry.store.terminal_bindings.get = lambda participant_id: None
    # Native delivery health is not focus evidence.  The presence monitor must
    # not turn it into permission to mutate a potentially attended session.
    registry.store.get_runtime_binding = lambda participant_id: SimpleNamespace(health="connected")
    snapshot = monitor.snapshot("participant-a")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "no-terminal-presence-evidence"
    with pytest.raises(HumanPresent):
        await monitor.require_absent("participant-a")


async def test_expired_provider_evidence_fails_closed(provider_monitor) -> None:
    monitor, service, _registry, clock = provider_monitor
    service.responses.append(result("absent", 1))
    await monitor.refresh()
    clock.value += 6
    snapshot = monitor.snapshot("participant-a")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "provider-evidence-stale"


async def test_expired_provider_evidence_publishes_unknown_once(provider_monitor) -> None:
    monitor, service, _registry, clock = provider_monitor
    changes: list[str] = []
    monitor._on_change = changes.append
    service.responses.append(result("absent", 1))
    await monitor.refresh()
    assert changes == ["participant-a"]

    clock.value += 6
    service.responses.append(RuntimeError("provider unavailable"))
    await monitor.refresh()
    assert monitor.snapshot("participant-a").state is PresenceState.UNKNOWN
    assert changes == ["participant-a", "participant-a"]

    service.responses.append(RuntimeError("provider still unavailable"))
    await monitor.refresh()
    assert changes == ["participant-a", "participant-a"]


async def test_successful_refresh_publishes_expiry_before_recovery(provider_monitor) -> None:
    monitor, service, _registry, clock = provider_monitor
    states: list[PresenceState] = []
    monitor._on_change = lambda participant_id: states.append(
        monitor.snapshot(participant_id).state
    )
    service.responses.append(result("absent", 1))
    await monitor.refresh()

    clock.value += 6
    service.responses.append(result("absent", 2))
    await monitor.refresh()

    assert states == [PresenceState.ABSENT, PresenceState.UNKNOWN, PresenceState.ABSENT]


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
    service.responses.append(RuntimeError("an unavailable inspector cannot prove exit"))
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
        _explicit_kills={participant.id},
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

    assert await retire_authoritative_exit(daemon, evidence) is False
    assert not registry.marked_dead
    daemon._explicit_kills.clear()
    assert await retire_authoritative_exit(daemon, evidence) is True
    assert registry.marked_dead == ["participant-a"]
    assert released

    registry.marked_dead.clear()
    replaced = replace(evidence, terminal_incarnation="replacement")
    assert await retire_authoritative_exit(daemon, replaced) is False
    assert registry.marked_dead == []


@pytest.mark.parametrize("harness", ["claude", "codex", "opencode", "pi", "vibe"])
async def test_terminal_exit_retires_participant_and_running_jobs(monkeypatch, harness):
    from theater.daemon.server import Daemon
    from theater.frontend.schemas import validate_callback_request, validate_callback_response

    daemon = Daemon(harnesses={})
    generation = 8 if harness == "codex" else 7
    try:
        participant = daemon.registry.register(harness=harness, pane=None, cwd=None)
        recorded = replace(
            binding(),
            participant_id=participant.id,
            health="missing" if generation == 8 else "healthy",
        )
        with daemon.store.write_unit() as unit:
            daemon.store.providers.register(
                ProviderRecord(
                    provider_id="provider-a",
                    selector="test",
                    kind="test",
                    credential_verifier="f" * 64,
                    configuration_version=1,
                    capabilities=("terminal-provider.v1",),
                    limits={},
                    generation=generation,
                    last_report_revision=0,
                    created_at=now(),
                    updated_at=now(),
                ),
                connection=unit.connection,
            )
            daemon.store.terminal_bindings.bind(recorded, connection=unit.connection)

        async def request(provider_id, requested_generation, method, params):
            assert (provider_id, requested_generation, method) == (
                "provider-a",
                generation,
                "terminal.inspect",
            )
            validate_callback_request(
                {"type": "request", "id": "cb", "method": method, "params": params}
            )
            response = result(
                "absent",
                1,
                lifecycle={"alive": False, "authoritative": True, "reason": "terminal_missing"},
            )
            response["provider_generation"] = generation
            response["terminal"]["provider_generation"] = generation
            assert params["expected_terminal"] == response["terminal"]
            validate_callback_response(method, {"type": "response", "id": "cb", "result": response})
            return response

        connections = daemon.terminal_service.connections
        monkeypatch.setattr(connections, "request", request)
        monkeypatch.setattr(connections, "is_current", lambda _provider, value: value == generation)
        monkeypatch.setattr(connections, "current_generation", lambda _provider: generation)
        monkeypatch.setattr(connections, "health", lambda _provider: "online")
        monkeypatch.setattr(connections, "renew", lambda *_: None)
        daemon.presence.configure_terminal_service(
            daemon.terminal_service,
            exit_handler=lambda evidence: retire_authoritative_exit(daemon, evidence),
        )
        job = daemon.jobs.create(
            handle="active-job", caller_id="cli", target_id=participant.id, kind="send"
        )
        await daemon.presence.refresh()

        assert daemon.store.get_participant(participant.id).status is Status.DEAD
        assert daemon.store.get_job(job.handle).state == JobState.CRASHED
        assert not daemon.registry.list()
    finally:
        await daemon.aclose()


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
