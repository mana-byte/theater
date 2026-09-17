"""Focused RC10 provider registry and public lifecycle checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from theater import paths
from theater.daemon.operations import OperationService
from theater.daemon.persistence.database import Database
from theater.daemon.persistence.repositories.journal import JournalRepository
from theater.daemon.persistence.repositories.operations import OperationRepository
from theater.daemon.persistence.repositories.providers import ProviderRepository
from theater.daemon.persistence.repositories.terminal_bindings import TerminalBindingRepository
from theater.daemon.plugins.credentials import credential_verifier
from theater.daemon.terminals import ProviderBusy, StaleGeneration, TerminalProviderService
from theater.daemon.terminals.bindings import TerminalIdentityMismatch
from theater.daemon.terminals.registry import ProviderRegistryConflict
from theater.daemon.terminals.service import StaleReportRevision
from theater.frontend import FrontendClient
from theater.models import PublicOperationRecord, TerminalBindingRecord


class _Store:
    def __init__(self, path: Path) -> None:
        self.db = Database(path)
        self.providers = ProviderRepository(self.db)
        self.terminal_bindings = TerminalBindingRepository(self.db)
        self.operations = OperationRepository(self.db)
        self.journal = JournalRepository(self.db)

    def write_unit(self):
        return self.db.write_unit()

    def close(self) -> None:
        self.db.close()


class _Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _service(store: _Store, clock: _Clock) -> TerminalProviderService:
    return TerminalProviderService(
        store,
        OperationService(store, clock=clock, id_factory=lambda: "operation-a"),
        monotonic=clock,
        clock=clock,
        id_factory=lambda: "provider-a",
    )


def _register(service: TerminalProviderService, *, key: str = "register-a") -> dict:
    result = service.registry.register(
        client_id="operator-a",
        idempotency_key=key,
        params={
            "selector": "fixture",
            "kind": "test",
            "credential_verifier": credential_verifier("credential-a"),
            "capabilities": ["terminal-provider.v1"],
            "limits": {"terminals": 10},
        },
    )
    assert isinstance(result, dict)
    return result


def _identity(generation: int, *, occupant: str = "occupant-a") -> dict:
    return {
        "provider_id": "provider-a",
        "provider_generation": generation,
        "terminal_id": "terminal-a",
        "terminal_incarnation": "incarnation-a",
        "occupant": {"occupant_id": occupant},
        "process": {"pid": 42, "started_at": 2.0, "executable": "/bin/agent"},
    }


def test_registration_is_idempotent_and_never_projects_the_verifier(tmp_path: Path) -> None:
    store = _Store(tmp_path / "providers.db")
    clock = _Clock(1.0)
    service = _service(store, clock)
    try:
        first = _register(service)
        replay = _register(service)
        assert first == replay
        assert first["provider_id"] == "provider-a"
        assert first["health"] == "offline"
        assert "credential" not in first and "credential_verifier" not in first

        with pytest.raises(ProviderRegistryConflict):
            service.registry.register(
                client_id="operator-a",
                idempotency_key="register-b",
                params={
                    "selector": "fixture",
                    "kind": "other",
                    "credential_verifier": "b" * 64,
                    "capabilities": [],
                    "limits": {},
                },
            )
    finally:
        store.close()


async def test_public_provider_registration_and_listing_use_frozen_sdk(daemon) -> None:
    client = FrontendClient(paths.socket_path(), client_id="operator-provider-test")
    try:
        registered = await client.providers.register(
            "fixture",
            "test",
            credential_verifier("credential-a"),
            ["terminal-provider.v1"],
            {"terminals": 10},
            idempotency_key="register-a",
        )
        replay = await client.providers.register(
            "fixture",
            "test",
            credential_verifier("credential-a"),
            ["terminal-provider.v1"],
            {"terminals": 10},
            idempotency_key="register-a",
        )
        providers = await client.providers.list()

        assert replay.value.provider_id == registered.value.provider_id
        assert providers.value.items == (registered.value,)
        assert "credential_verifier" not in registered.value.extra
    finally:
        await client.close()


def test_callback_generation_report_and_restart_health_are_fenced(tmp_path: Path) -> None:
    store = _Store(tmp_path / "generation.db")
    clock = _Clock(10.0)
    service = _service(store, clock)
    try:
        _register(service)
        generation, token = service.connections.acquire_callback("provider-a", "credential-a")
        assert token
        assert generation == 1
        projected = service.registry.project(service.registry.get("provider-a"))
        assert projected["health"] == "reconciling"
        with pytest.raises(ProviderBusy):
            service.connections.acquire_callback("provider-a", "credential-a")
        assert service.connections.rpc_generation("provider-a", "credential-a") == generation

        service.heartbeat("provider-a", generation, 1)
        assert service.connections.health("provider-a") == "reconciling"
        service.report("provider-a", generation, 2, {"terminals": [], "complete": True})
        assert service.connections.health("provider-a") == "online"
        with pytest.raises(StaleReportRevision):
            service.heartbeat("provider-a", generation, 2)

        clock.value += 31
        assert service.connections.health("provider-a") == "offline"
        replacement_generation, _ = service.connections.acquire_callback(
            "provider-a", "credential-a"
        )
        assert replacement_generation == 2
        with pytest.raises(StaleGeneration):
            service.heartbeat("provider-a", generation, 3)

        restarted = _service(store, clock)
        assert restarted.connections.health("provider-a") == "offline"
        assert (
            restarted.connections.rpc_generation("provider-a", "credential-a")
            == replacement_generation
        )
    finally:
        store.close()


def test_report_restores_only_an_exact_terminal_identity(tmp_path: Path) -> None:
    store = _Store(tmp_path / "bindings.db")
    clock = _Clock(10.0)
    service = _service(store, clock)
    params = {
        "selector": "fixture",
        "kind": "test",
        "credential_verifier": credential_verifier("credential-a"),
        "capabilities": ["terminal-provider.v1"],
        "limits": {},
    }
    service.registry.register(client_id="operator-a", idempotency_key="register-a", params=params)
    with store.write_unit() as unit:
        store.terminal_bindings.bind(
            TerminalBindingRecord(
                participant_id="participant-a",
                provider_id="provider-a",
                provider_generation=0,
                terminal_id="terminal-a",
                terminal_incarnation="incarnation-a",
                occupant_evidence={"occupant_id": "occupant-a"},
                process_facts={"pid": 42, "started_at": 2.0, "executable": "/bin/agent"},
                health="reconciling",
                report_revision=0,
                created_at=1.0,
                updated_at=1.0,
            ),
            connection=unit.connection,
        )
    generation, _token = service.connections.acquire_callback("provider-a", "credential-a")
    restored = service.report(
        "provider-a",
        generation,
        1,
        {"terminals": [_identity(generation)], "complete": True},
    )
    assert restored["restored_participant_ids"] == ["participant-a"]
    assert store.terminal_bindings.get("participant-a").provider_generation == generation

    with pytest.raises(TerminalIdentityMismatch):
        service.report(
            "provider-a",
            generation,
            2,
            {
                "terminals": [_identity(generation, occupant="replacement")],
                "complete": True,
            },
        )
    assert store.providers.get("provider-a").last_report_revision == 1
    service.connections.disconnect("provider-a", generation)
    binding = store.terminal_bindings.get("participant-a")
    assert binding is not None
    assert service.binding_projection(binding)["health"] == "offline"
    store.close()


def test_current_provider_can_reconcile_an_exact_historical_receipt(tmp_path: Path) -> None:
    store = _Store(tmp_path / "receipts.db")
    clock = _Clock(10.0)
    service = _service(store, clock)
    _register(service)
    with store.write_unit() as unit:
        store.operations.create(
            PublicOperationRecord(
                operation_id="operation-receipt",
                kind="send",
                actor_client_id="operator-a",
                actor_participant_id=None,
                target_ids=("participant-a",),
                state="uncertain",
                phase="provider_ack_lost",
                created_at=1.0,
                updated_at=2.0,
                dispatch_provider_id="provider-a",
                dispatch_provider_generation=0,
                dispatch_terminal_id="terminal-a",
                dispatch_terminal_incarnation="incarnation-a",
                dispatch_terminal_occupant_evidence={"occupant_id": "occupant-a"},
                error_code="provider_unavailable",
                error={"code": "provider_unavailable", "message": "acknowledgment lost"},
            ),
            connection=unit.connection,
        )
    generation, _ = service.connections.acquire_callback("provider-a", "credential-a")

    report = service.report(
        "provider-a",
        generation,
        1,
        {
            "receipts": [
                {
                    "operation_id": "operation-receipt",
                    "provider_generation": 0,
                    "terminal_id": "terminal-a",
                    "terminal_incarnation": "incarnation-a",
                    "delivery": "accepted",
                }
            ]
        },
    )

    assert report["reconciled_operation_ids"] == ["operation-receipt"]
    assert store.operations.get("operation-receipt").state == "succeeded"
    store.close()
