"""ProviderClient callbacks: handshake, serialization, fencing and loss handling."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from tests.support.provider_callback_daemon import (
    connect_provider,
    create_frame,
    deliver_frame,
    error_code,
    eventually,
    fixture_daemon,
    frame_id,
    inspect_frame,
    inventory_frame,
    mapping_value,
    provider_client,
    result_for,
)
from tests.support.provider_records import provider_record
from theater import paths
from theater.daemon.operations import DispatchIntent, PreparedOperation
from theater.daemon.plugins.credentials import credential_verifier
from theater.daemon.terminals import CallbackOutcomeUnknown, ProviderBusy, ProviderUnavailable
from theater.frontend import FrontendClient
from theater.frontend.provider import (
    CallbackRequest,
    CallbackResponse,
    ProviderClient,
    ProviderProtocolError,
)
from theater.models import ProviderRecord, PublicOperationRecord, TerminalBindingRecord, now


async def test_provider_connects_with_callback_handshake_and_preserves_future_values() -> None:
    async def handler(request: CallbackRequest) -> Mapping[str, object] | CallbackResponse:
        if request.method == "terminal.inventory":
            return {**result_for(request), "future_result_field": {"kept": True}}
        return CallbackResponse(
            error={
                "code": "future_provider_refusal",
                "message": "A newer provider rejected inspection.",
                "future_error_field": ["kept"],
            }
        )

    async with fixture_daemon() as daemon:
        client = provider_client(
            daemon.socket_path, {"terminal.inventory": handler, "terminal.inspect": handler}
        )
        session = await connect_provider(client, daemon)
        assert client.handshake_result is not None
        assert client.handshake_result.extra["future_handshake_field"] == {"kept": True}

        await session.send(inventory_frame("inventory-a"))
        inventory = await session.read()
        await session.send(inspect_frame("inspect-a"))
        refusal = await session.read()
        await client.close()

    assert mapping_value(inventory, "result")["future_result_field"] == {"kept": True}
    assert error_code(refusal) == "future_provider_refusal"
    assert mapping_value(refusal, "error")["future_error_field"] == ["kept"]


async def test_independent_terminal_callbacks_run_concurrently_without_blocking_reads() -> None:
    started: set[str] = set()
    both_started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: CallbackRequest) -> Mapping[str, object]:
        if request.method == "terminal.inventory":
            return result_for(request)
        terminal_id = request.params["terminal_id"]
        assert isinstance(terminal_id, str)
        started.add(terminal_id)
        if started == {"terminal-a", "terminal-b"}:
            both_started.set()
        await release.wait()
        return result_for(request)

    async with fixture_daemon() as daemon:
        client = provider_client(
            daemon.socket_path, {"terminal.deliver": handler, "terminal.inventory": handler}
        )
        session = await connect_provider(client, daemon)
        await session.send(deliver_frame("deliver-a", terminal_id="terminal-a"))
        await session.send(deliver_frame("deliver-b", terminal_id="terminal-b"))
        await asyncio.wait_for(both_started.wait(), timeout=1)
        await session.send(inventory_frame("inventory-while-waiting"))
        inventory = await session.read()
        assert frame_id(inventory) == "inventory-while-waiting"
        release.set()
        responses = {frame_id(await session.read()) for _ in range(2)}
        await client.close()

    assert responses == {"deliver-a", "deliver-b"}


@pytest.mark.parametrize(
    "first,second",
    [
        (deliver_frame("first"), deliver_frame("second")),
        (create_frame("first"), create_frame("second")),
    ],
)
async def test_same_identity_mutations_serialize_before_their_handler_starts(
    first: dict[str, object], second: dict[str, object]
) -> None:
    active = 0
    maximum_active = 0
    order: list[str] = []
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()

    async def handler(request: CallbackRequest) -> Mapping[str, object]:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        order.append(request.callback_id)
        if request.callback_id == "first":
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        active -= 1
        return result_for(request)

    async with fixture_daemon() as daemon:
        client = provider_client(
            daemon.socket_path,
            {"terminal.deliver": handler, "terminal.create": handler},
        )
        session = await connect_provider(client, daemon)
        await session.send(first)
        await session.send(second)
        await asyncio.wait_for(first_started.wait(), timeout=1)
        await eventually(lambda: client.pending_callbacks == 2)
        assert not second_started.is_set()
        release_first.set()
        await asyncio.wait_for(second_started.wait(), timeout=1)
        response_ids = {frame_id(await session.read()) for _ in range(2)}
        await client.close()

    assert order == ["first", "second"]
    assert maximum_active == 1
    assert response_ids == {"first", "second"}


async def test_saturation_refuses_before_dispatch() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def handler(request: CallbackRequest) -> Mapping[str, object]:
        calls.append(request.callback_id)
        started.set()
        await release.wait()
        return result_for(request)

    async with fixture_daemon(limits={"provider_pending_callbacks": 1}) as daemon:
        client = provider_client(daemon.socket_path, {"terminal.inventory": handler})
        session = await connect_provider(client, daemon)
        await session.send(inventory_frame("slow"))
        await asyncio.wait_for(started.wait(), timeout=1)
        await session.send(inventory_frame("slow"))
        await asyncio.sleep(0)
        assert calls == ["slow"]
        await session.send(inventory_frame("saturated"))
        refusal = await session.read()
        assert frame_id(refusal) == "saturated"
        assert error_code(refusal) == "provider_busy"
        assert calls == ["slow"]
        release.set()
        completed = await session.read()
        await client.close()

    assert frame_id(completed) == "slow"


async def test_generation_loss_drops_a_same_terminal_callback_before_dispatch() -> None:
    started: list[str] = []
    first_started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: CallbackRequest) -> Mapping[str, object]:
        started.append(request.callback_id)
        if request.callback_id == "first":
            first_started.set()
            await release.wait()
        return result_for(request)

    async with fixture_daemon() as daemon:
        client = provider_client(daemon.socket_path, {"terminal.deliver": handler})
        session = await connect_provider(client, daemon)
        await session.send(deliver_frame("first"))
        await session.send(deliver_frame("stale"))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        await eventually(lambda: client.pending_callbacks == 2)
        client.invalidate_generation(generation=7)
        release.set()
        responses = {
            frame_id(response): response
            for response in (await session.read(), await session.read())
        }
        await client.close()

    assert started == ["first"]
    assert mapping_value(responses["first"], "result")["delivery"] == "unknown"
    assert error_code(responses["stale"]) == "stale_generation"


async def test_timed_out_mutation_duplicate_replays_unknown_during_handler() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(request: CallbackRequest) -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return result_for(request)

    async with fixture_daemon() as daemon:
        client = ProviderClient(
            daemon.socket_path,
            client_id="provider-fixture",
            provider_id="provider-a",
            provider_credential="credential-a",
            handlers={"terminal.deliver": handler},
            callback_timeout=0.01,
        )
        session = await connect_provider(client, daemon)
        request = deliver_frame("timed-out")
        await session.send(request)
        await asyncio.wait_for(started.wait(), timeout=1)
        first = await session.read()
        assert mapping_value(first, "result")["delivery"] == "unknown"

        await session.send(request)
        replay = await asyncio.wait_for(session.read(), timeout=0.1)
        assert replay == first
        assert calls == 1

        release.set()
        await eventually(lambda: client.pending_callbacks == 0)
        await client.close()


async def test_started_mutation_survives_connection_loss_without_a_rollback_promise() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(request: CallbackRequest) -> Mapping[str, object]:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finished.set()
        return result_for(request)

    async with fixture_daemon() as daemon:
        client = provider_client(daemon.socket_path, {"terminal.deliver": handler})
        session = await connect_provider(client, daemon)
        await session.send(deliver_frame("loss"))
        await asyncio.wait_for(started.wait(), timeout=1)
        await session.close()
        await asyncio.wait_for(client.wait_closed(), timeout=1)
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=1)
        await client.close()

    assert not cancelled.is_set()


async def test_cancelled_mutation_and_late_or_unknown_frames_are_safe() -> None:
    calls = 0

    async def handler(request: CallbackRequest) -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        if request.callback_id == "cancelled":
            raise asyncio.CancelledError
        return result_for(request)

    async with fixture_daemon() as daemon:
        client = provider_client(
            daemon.socket_path, {"terminal.deliver": handler, "terminal.inventory": handler}
        )
        session = await connect_provider(client, daemon)
        await session.send(deliver_frame("cancelled"))
        cancelled = await session.read()
        assert mapping_value(cancelled, "result")["delivery"] == "unknown"

        await session.send(
            {"type": "request", "id": "unknown", "method": "terminal.future", "params": {}}
        )
        unknown = await session.read()
        assert error_code(unknown) == "unknown_method"

        await session.send(inventory_frame("duplicate"))
        first = await session.read()
        await session.send(inventory_frame("duplicate"))
        duplicate = await session.read()
        assert first == duplicate
        assert calls == 2

        await session.send({"type": "response", "id": "late", "result": {}})
        await asyncio.wait_for(client.wait_closed(), timeout=1)
        assert isinstance(client.last_error, ProviderProtocolError)
        await client.close()


async def test_negotiated_frame_limits_reject_oversized_inbound_and_outbound_frames() -> None:
    async def large_handler(request: CallbackRequest) -> Mapping[str, object]:
        return {**result_for(request), "future_payload": "x" * 1_024}

    async with fixture_daemon(limits={"max_frame_bytes": 512}) as daemon:
        client = provider_client(daemon.socket_path, {"terminal.inventory": large_handler})
        session = await connect_provider(client, daemon)
        await session.send(inventory_frame("outbound"))
        too_large = await session.read()
        assert error_code(too_large) == "too_large"
        await client.close()

        inbound = provider_client(daemon.socket_path, {"terminal.inventory": large_handler})
        session = await connect_provider(inbound, daemon)
        await session.send_raw(
            b'{"type":"request","id":"oversized","payload":"' + b"x" * 600 + b'"}\n'
        )
        await asyncio.wait_for(inbound.wait_closed(), timeout=1)
        assert isinstance(inbound.last_error, ProviderProtocolError)
        await inbound.close()


def _record(*, limits: dict[str, object] | None = None) -> ProviderRecord:
    return provider_record(
        selector="fixture",
        kind="test",
        credential_verifier=credential_verifier("credential-a"),
        limits=limits,
        generation=0,
        last_report_revision=None,
    )


def _deliver_params(operation_id: str, terminal_id: str) -> dict[str, object]:
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
                "provider-a", 1, "terminal.deliver", _deliver_params("operation-a", "terminal-a")
            )
        )
        await first_started.wait()
        second = asyncio.create_task(
            daemon.terminal_service.connections.request(
                "provider-a", 1, "terminal.deliver", _deliver_params("operation-b", "terminal-a")
            )
        )
        independent = asyncio.create_task(
            daemon.terminal_service.connections.request(
                "provider-a", 1, "terminal.deliver", _deliver_params("operation-c", "terminal-b")
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
            _deliver_params(operation["id"], "terminal-a"),
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
            _deliver_params("operation-a", "terminal-a"),
        )

    assert caught.value.details["possibly_executed"] is True
    assert len(writer.frames) == 1
    assert writer.closed is True
    with pytest.raises(ProviderUnavailable, match="offline"):
        await daemon.terminal_service.connections.request(
            "provider-a",
            generation,
            "terminal.deliver",
            _deliver_params("operation-a", "terminal-a"),
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
                "provider-a", 1, "terminal.deliver", _deliver_params("operation-a", "terminal-a")
            )
        )
        await started.wait()
        with pytest.raises(ProviderBusy):
            await daemon.terminal_service.connections.request(
                "provider-a", 1, "terminal.deliver", _deliver_params("operation-b", "terminal-b")
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
        params = _deliver_params("operation-c", "terminal-c")
        params["provider_generation"] = generation
        with pytest.raises(CallbackOutcomeUnknown):
            await daemon.terminal_service.connections.request(
                "provider-a", generation, "terminal.deliver", params
            )
    finally:
        await replacement.close()
