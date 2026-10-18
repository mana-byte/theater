"""Focused duplex callback and generation fencing checks."""

from __future__ import annotations

import asyncio

import pytest

from theater import paths
from theater.daemon.operations import DispatchIntent, PreparedOperation
from theater.daemon.plugins.credentials import credential_verifier
from theater.daemon.terminals import CallbackOutcomeUnknown, ProviderBusy, ProviderUnavailable
from theater.frontend import FrontendClient
from theater.frontend.provider import CallbackRequest, ProviderClient
from theater.models import ProviderRecord, PublicOperationRecord, TerminalBindingRecord, now


def _record(*, limits: dict[str, object] | None = None) -> ProviderRecord:
    timestamp = now()
    return ProviderRecord(
        provider_id="provider-a",
        selector="fixture",
        kind="test",
        credential_verifier=credential_verifier("credential-a"),
        configuration_version=1,
        capabilities=("terminal-provider.v1",),
        limits=limits or {},
        generation=0,
        last_report_revision=None,
        created_at=timestamp,
        updated_at=timestamp,
    )


def _deliver(operation_id: str, terminal_id: str) -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "provider_generation": 1,
        "participant_id": f"participant-{terminal_id}",
        "terminal_id": terminal_id,
        "terminal_incarnation": "incarnation-a",
        "expected_occupant": "occupant-a",
        "action": {"kind": "submit_text", "text": "hello"},
        "require_absent": True,
    }


async def _wait_offline(daemon) -> None:
    async with asyncio.timeout(1):
        while daemon.terminal_service.connections.health("provider-a") != "offline":
            await asyncio.sleep(0)


class _DrainFailureWriter:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.closed = False

    def write(self, frame: bytes) -> None:
        self.frames.append(frame)

    async def drain(self) -> None:
        raise ConnectionResetError("connection failed after accepting bytes")

    def close(self) -> None:
        self.closed = True


async def test_duplex_callbacks_serialize_one_terminal_but_not_inventory(daemon) -> None:
    with daemon.store.write_unit() as unit:
        daemon.store.providers.register(_record(), connection=unit.connection)
    first_started = asyncio.Event()
    independent_started = asyncio.Event()
    release = asyncio.Event()
    started: list[str] = []

    async def deliver(request: CallbackRequest) -> dict[str, object]:
        operation_id = str(request.params["operation_id"])
        started.append(operation_id)
        if operation_id == "operation-a":
            first_started.set()
            await release.wait()
        if operation_id == "operation-c":
            independent_started.set()
        return {
            "operation_id": operation_id,
            "provider_generation": request.provider_generation,
            "terminal_id": request.params["terminal_id"],
            "terminal_incarnation": request.params["terminal_incarnation"],
            "delivery": "accepted",
        }

    async def inventory(request: CallbackRequest) -> dict[str, object]:
        return {
            "provider_generation": request.provider_generation,
            "report_revision": 1,
            "complete": True,
            "terminals": [],
        }

    client = ProviderClient(
        str(paths.socket_path()),
        client_id="provider-client",
        provider_id="provider-a",
        provider_credential="credential-a",
        handlers={"terminal.deliver": deliver, "terminal.inventory": inventory},
    )
    try:
        result = await client.connect()
        assert result.provider_generation == 1
        rpc = FrontendClient(
            paths.socket_path(),
            client_id="provider-reporter",
            role="provider",
            provider_id="provider-a",
            provider_credential="credential-a",
        )
        heartbeat = await rpc.providers.heartbeat(1, 1)
        assert heartbeat.value["health"] == "reconciling"
        report = await rpc.providers.report(1, 2, facts={"terminals": [], "complete": True})
        assert report.value["health"] == "online"
        client.renew_lease(generation=1)
        await rpc.close()
        first = asyncio.create_task(
            daemon.terminal_service.connections.request(
                "provider-a", 1, "terminal.deliver", _deliver("operation-a", "terminal-a")
            )
        )
        await first_started.wait()
        second = asyncio.create_task(
            daemon.terminal_service.connections.request(
                "provider-a", 1, "terminal.deliver", _deliver("operation-b", "terminal-a")
            )
        )
        independent = asyncio.create_task(
            daemon.terminal_service.connections.request(
                "provider-a", 1, "terminal.deliver", _deliver("operation-c", "terminal-b")
            )
        )
        await independent_started.wait()
        await independent
        inventory_result = await daemon.terminal_service.connections.request(
            "provider-a", 1, "terminal.inventory", {"provider_generation": 1}
        )
        assert inventory_result["complete"] is True
        await asyncio.sleep(0)
        assert started == ["operation-a", "operation-c"]
        release.set()
        await asyncio.gather(first, second)
        assert started == ["operation-a", "operation-c", "operation-b"]
    finally:
        await client.close()


async def test_disconnect_after_mutation_dispatch_is_uncertain_and_reconnects(daemon) -> None:
    with daemon.store.write_unit() as unit:
        daemon.store.providers.register(_record(), connection=unit.connection)
    started = asyncio.Event()
    never = asyncio.Event()

    async def deliver(_request: CallbackRequest) -> dict[str, object]:
        started.set()
        await never.wait()
        raise AssertionError

    client = ProviderClient(
        str(paths.socket_path()),
        client_id="provider-client",
        provider_id="provider-a",
        provider_credential="credential-a",
        handlers={"terminal.deliver": deliver},
    )
    await client.connect()
    timestamp = now()
    with daemon.store.write_unit() as unit:
        daemon.store.terminal_bindings.bind(
            TerminalBindingRecord(
                participant_id="participant-terminal-a",
                provider_id="provider-a",
                provider_generation=1,
                terminal_id="terminal-a",
                terminal_incarnation="incarnation-a",
                occupant_evidence={"occupant_id": "occupant-a"},
                health="healthy",
                report_revision=1,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
    operation: dict[str, str] = {}

    def prepare(operation_id, _unit):
        operation["id"] = operation_id
        return PreparedOperation(
            PublicOperationRecord(
                operation_id=operation_id,
                kind="send",
                actor_client_id="operator-a",
                actor_participant_id=None,
                target_ids=("participant-terminal-a",),
                state="accepted",
                phase="accepted",
                created_at=timestamp,
                updated_at=timestamp,
            ),
            {"operation_id": operation_id, "state": "accepted"},
        )

    async def dispatch():
        return await daemon.terminal_service.dispatch_operation(
            "provider-a",
            1,
            "terminal.deliver",
            _deliver(operation["id"], "terminal-a"),
        )

    acceptance = daemon.operation_service.submit(
        client_id="operator-a",
        idempotency_key="send-a",
        method="frontend.controls.send",
        params={"participant_id": "participant-terminal-a", "prompt": "hello"},
        prepare=prepare,
        dispatch=DispatchIntent(
            phase="provider_dispatch",
            provider_id="provider-a",
            provider_generation=1,
            terminal_id="terminal-a",
            terminal_incarnation="incarnation-a",
            occupant_evidence={"occupant_id": "occupant-a"},
        ),
        side_effect=dispatch,
    )
    await started.wait()
    await client.close()
    await asyncio.gather(*daemon.operation_service.owned_tasks)
    assert daemon.operation_service.get(acceptance.record.operation_id).state == "uncertain"
    await _wait_offline(daemon)

    replacement = ProviderClient(
        str(paths.socket_path()),
        client_id="provider-client-2",
        provider_id="provider-a",
        provider_credential="credential-a",
        handlers={},
    )
    try:
        result = await replacement.connect()
        assert result.provider_generation == 2
        with pytest.raises(ProviderBusy):
            daemon.terminal_service.connections.acquire_callback("provider-a", "credential-a")
    finally:
        await replacement.close()


async def test_mutating_drain_failure_after_write_is_uncertain_and_never_replayed(daemon) -> None:
    with daemon.store.write_unit() as unit:
        daemon.store.providers.register(_record(), connection=unit.connection)
    generation, _ = daemon.terminal_service.connections.acquire_callback(
        "provider-a", "credential-a"
    )
    peer = daemon.terminal_service.connections._peers["provider-a"]
    writer = _DrainFailureWriter()
    peer.writer = writer
    peer.attached = True

    with pytest.raises(CallbackOutcomeUnknown) as caught:
        await daemon.terminal_service.connections.request(
            "provider-a",
            generation,
            "terminal.deliver",
            _deliver("operation-a", "terminal-a"),
        )

    assert caught.value.details["possibly_executed"] is True
    assert len(writer.frames) == 1
    assert writer.closed is True
    with pytest.raises(ProviderUnavailable, match="offline"):
        await daemon.terminal_service.connections.request(
            "provider-a",
            generation,
            "terminal.deliver",
            _deliver("operation-a", "terminal-a"),
        )
    assert len(writer.frames) == 1


async def test_create_result_with_foreign_terminal_identity_is_uncertain(daemon) -> None:
    with daemon.store.write_unit() as unit:
        daemon.store.providers.register(_record(), connection=unit.connection)
        daemon.store.operations.create(
            PublicOperationRecord(
                operation_id="operation-create",
                kind="spawn",
                actor_client_id="operator-a",
                actor_participant_id=None,
                target_ids=("participant-a",),
                state="running",
                phase="provider_dispatch",
                created_at=now(),
                updated_at=now(),
            ),
            connection=unit.connection,
        )

    async def create(request: CallbackRequest) -> dict[str, object]:
        return {
            "operation_id": request.params["operation_id"],
            "provider_generation": request.provider_generation,
            "outcome": "accepted",
            "terminal": {
                "provider_id": "provider-b",
                "provider_generation": request.provider_generation,
                "terminal_id": "terminal-a",
                "terminal_incarnation": "incarnation-a",
                "occupant": {"occupant_id": "occupant-a"},
            },
        }

    client = ProviderClient(
        str(paths.socket_path()),
        client_id="foreign-identity-provider",
        provider_id="provider-a",
        provider_credential="credential-a",
        handlers={"terminal.create": create},
    )
    try:
        await client.connect()
        outcome = await daemon.terminal_service.dispatch_operation(
            "provider-a",
            1,
            "terminal.create",
            {
                "operation_id": "operation-create",
                "provider_generation": 1,
                "participant_id": "participant-a",
                "launch_id": "launch-a",
                "launch": {
                    "executable": "/bin/agent",
                    "argv": ["agent"],
                    "cwd": "/tmp",
                    "environment": {},
                },
            },
        )
        assert (outcome.state, outcome.phase) == ("uncertain", "provider_ack_lost")
        await _wait_offline(daemon)
    finally:
        await client.close()


async def test_negotiated_pending_bound_and_result_generation_are_enforced(daemon) -> None:
    with daemon.store.write_unit() as unit:
        daemon.store.providers.register(
            _record(limits={"provider_pending_callbacks": 1}), connection=unit.connection
        )
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked(request: CallbackRequest) -> dict[str, object]:
        started.set()
        await release.wait()
        return {
            "operation_id": request.params["operation_id"],
            "provider_generation": request.provider_generation,
            "terminal_id": request.params["terminal_id"],
            "terminal_incarnation": request.params["terminal_incarnation"],
            "delivery": "accepted",
        }

    client = ProviderClient(
        str(paths.socket_path()),
        client_id="bounded-provider",
        provider_id="provider-a",
        provider_credential="credential-a",
        handlers={"terminal.deliver": blocked},
    )
    try:
        handshake = await client.connect()
        assert handshake.limits["provider_pending_callbacks"] == 1
        first = asyncio.create_task(
            daemon.terminal_service.connections.request(
                "provider-a", 1, "terminal.deliver", _deliver("operation-a", "terminal-a")
            )
        )
        await started.wait()
        with pytest.raises(ProviderBusy):
            await daemon.terminal_service.connections.request(
                "provider-a", 1, "terminal.deliver", _deliver("operation-b", "terminal-b")
            )
        release.set()
        await first
    finally:
        await client.close()

    await _wait_offline(daemon)

    async def wrong_generation(request: CallbackRequest) -> dict[str, object]:
        return {
            "operation_id": request.params["operation_id"],
            "provider_generation": request.provider_generation + 1,
            "terminal_id": request.params["terminal_id"],
            "terminal_incarnation": request.params["terminal_incarnation"],
            "delivery": "accepted",
        }

    replacement = ProviderClient(
        str(paths.socket_path()),
        client_id="wrong-generation-provider",
        provider_id="provider-a",
        provider_credential="credential-a",
        handlers={"terminal.deliver": wrong_generation},
    )
    try:
        result = await replacement.connect()
        generation = result.provider_generation
        assert generation == 2
        params = _deliver("operation-c", "terminal-c")
        params["provider_generation"] = generation
        with pytest.raises(CallbackOutcomeUnknown):
            await daemon.terminal_service.connections.request(
                "provider-a", generation, "terminal.deliver", params
            )
    finally:
        await replacement.close()
