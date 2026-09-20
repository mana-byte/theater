"""Provider-routed controls retain durable linkage and exact terminal fences."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, update

import theater.daemon.frontend.participant_mutation_handlers as mutation_handlers
from tests._presence_doubles import AbsentPresence, UnknownPresence
from tests.rig.fake_runtime import FakeRuntime, FakeRuntimeIO, FakeRuntimeState
from theater.daemon.controls.routing import ControlRouteResolver
from theater.daemon.frontend.control_handlers import (
    _error,
    controls_get,
    controls_interrupt,
    controls_queue_followup,
    controls_send,
    controls_settings_update,
    controls_steer,
)
from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.frontend.operation_handlers import operations_reconcile
from theater.daemon.frontend.participant_mutation_handlers import (
    participants_status,
    participants_terminate,
    participants_update,
)
from theater.daemon.frontend.participant_read_handlers import participant_to_wire
from theater.daemon.harness_runtime.errors import BackendIdentityMismatch
from theater.daemon.operations import operation_to_wire
from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.daemon.persistence.repositories.native_evidence import NativeTerminalEvidence
from theater.daemon.persistence.repositories.runtime_bindings import ParticipantRuntimeBinding
from theater.daemon.rpc import participants as participant_rpc
from theater.daemon.schema import orchestration_events, terminal_bindings, workspaces
from theater.daemon.terminals import CallbackOutcomeUnknown
from theater.frontend.capabilities import METHOD_CATALOG, ConnectionChannel, ConnectionRole
from theater.frontend.schemas import validate_callback_request, validator_for
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
    NativeTurnTerminal,
    ResultCompleteness,
    ResultProvenance,
    RuntimeCapability,
    RuntimeContext,
    RuntimeLifecyclePhase,
    RuntimeWiring,
)
from theater.models import (
    JobState,
    PublicOperationRecord,
    PublicOperationState,
    StaleTarget,
    Status,
    TerminalBindingRecord,
    TheaterError,
    Tier,
    WorkspaceOwnershipKind,
    WorkspaceRecord,
    WorkspaceState,
    WorkspaceUsageHolderKind,
    WorkspaceUsageRecord,
    now,
)


def _context() -> ConnectionContext:
    return ConnectionContext(
        client_id="operator-a",
        role=ConnectionRole.OPERATOR,
        channel=ConnectionChannel.RPC,
        api_major=1,
        api_minor=0,
        capabilities=frozenset({"orchestration.v1"}),
    )


def _bind(daemon, participant_id: str, *, generation: int = 1) -> None:
    timestamp = now()
    with daemon.store.write_unit() as unit:
        daemon.store.terminal_bindings.bind(
            TerminalBindingRecord(
                participant_id=participant_id,
                provider_id="provider-a",
                provider_generation=generation,
                terminal_id="terminal-a",
                terminal_incarnation="incarnation-a",
                occupant_evidence={"occupant_id": "occupant-a", "harness": "codex"},
                process_facts={"pid": 42, "started_at": 1.0, "executable": "/bin/agent"},
                health="healthy",
                report_revision=1,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )


def _target(daemon) -> str:
    participant = daemon.registry.register(harness="codex", pane=None, cwd=None)
    _bind(daemon, participant.id)
    return participant.id


async def _install_native_runtime(
    daemon, participant_id: str, *, generation: int, session_id: str
) -> tuple[FakeRuntime, FakeRuntimeState]:
    state = FakeRuntimeState(
        participant_id=participant_id,
        backend_generation=generation,
        native_session_id=session_id,
    )
    runtime = FakeRuntime(
        RuntimeContext(
            participant_id=participant_id,
            cwd=None,
            io=FakeRuntimeIO(state),
            backend_generation=generation,
        )
    )

    async def create() -> FakeRuntime:
        return runtime

    installed = await daemon.runtime_manager.get_or_create(
        participant_id, backend_generation=generation, create=create
    )
    assert installed is runtime
    return runtime, state


def _online(monkeypatch: pytest.MonkeyPatch, daemon) -> None:
    monkeypatch.setattr(daemon.terminal_service.connections, "is_current", lambda *_: True)
    monkeypatch.setattr(daemon.terminal_service.connections, "health", lambda *_: "online")

    async def inspect(provider_id, generation, terminal_id, incarnation):
        current = next(
            binding
            for binding in daemon.store.terminal_bindings.list_for_provider(provider_id)
            if binding.terminal_id == terminal_id
            and binding.terminal_incarnation == incarnation
            and binding.provider_generation == generation
        )
        revision = current.report_revision + 1
        with daemon.store.write_unit() as unit:
            assert daemon.store.terminal_bindings.update_health(
                current.participant_id,
                provider_generation=generation,
                report_revision=revision,
                health="healthy",
                updated_at=now(),
                connection=unit.connection,
            )
        return {
            "provider_generation": generation,
            "report_revision": revision,
            "terminal": {
                "provider_id": provider_id,
                "provider_generation": generation,
                "terminal_id": terminal_id,
                "terminal_incarnation": incarnation,
                "occupant": dict(current.occupant_evidence),
                "process": dict(current.process_facts or {}),
            },
            "presence": {
                "state": "absent",
                "revision": revision,
                "reason": "test-no-human-focus",
            },
        }

    monkeypatch.setattr(daemon.terminal_service, "inspect", inspect)


async def _settle(daemon) -> None:
    await asyncio.sleep(0)
    tasks = daemon.operation_service.owned_tasks
    if tasks:
        await asyncio.gather(*tasks)


async def _control_for_public_operation(daemon, operation_id: str):
    for _ in range(20):
        operation = daemon.operation_service.get(operation_id)
        if operation.control_operation_id is not None:
            return daemon.store.get_control_operation(operation.control_operation_id)
        await asyncio.sleep(0)
    return None


def _accepted(method: str, value: object) -> None:
    validator_for(METHOD_CATALOG[method].result_schema_id).validate(value)


async def _public_action_views(daemon, participant, *, actor: str):
    participant_value = await participant_to_wire(daemon, participant)
    controls_value = await controls_get(daemon, _context(), {"participant_id": participant.id})
    snapshot = daemon.state_service.snapshot(actor, page_size=500)
    snapshot_value = next(
        item for item in snapshot["participants"] if item["participant_id"] == participant.id
    )
    return participant_value, controls_value, snapshot_value


async def test_provider_send_links_one_public_operation_job_and_control(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = _target(daemon)
    _online(monkeypatch, daemon)
    requests: list[dict[str, object]] = []

    async def request(_provider, generation, method, params):
        frame = {"type": "request", "id": "callback-a", "method": method, "params": params}
        validate_callback_request(frame)
        requests.append(dict(params))
        return {
            "operation_id": params["operation_id"],
            "provider_generation": generation,
            "terminal_id": params["terminal_id"],
            "terminal_incarnation": params["terminal_incarnation"],
            "delivery": "accepted",
        }

    monkeypatch.setattr(daemon.terminal_service.connections, "request", request)
    params = {
        "participant_id": participant_id,
        "prompt": "review this",
        "response_format": {"type": "object"},
    }
    accepted = await controls_send(daemon, _context(), params, idempotency_key="provider-send-a")
    duplicate = await controls_send(daemon, _context(), params, idempotency_key="provider-send-a")
    _accepted("frontend.controls.send", accepted)
    assert duplicate == accepted
    await _settle(daemon)

    operation = daemon.operation_service.get(accepted["operation_id"])
    assert operation.state == PublicOperationState.SUCCEEDED.value, operation.error
    assert operation.control_operation_id == f"{operation.operation_id}:control"
    assert operation.job_handle is not None
    job = daemon.store.get_job(operation.job_handle)
    assert job.response_format == '{"type":"object"}'
    assert job.caller_id == "cli"
    assert job.actor_client_id == "operator-a"
    assert job.actor_participant_id is None
    control = daemon.store.get_control_operation(operation.control_operation_id)
    assert control is not None
    assert control.transport is ControlTransport.PROVIDER_TERMINAL
    assert (control.provider_id, control.provider_generation) == ("provider-a", 1)
    assert (control.terminal_id, control.terminal_incarnation) == (
        "terminal-a",
        "incarnation-a",
    )
    assert requests == [
        {
            "operation_id": operation.operation_id,
            "provider_generation": 1,
            "participant_id": participant_id,
            "terminal_id": "terminal-a",
            "terminal_incarnation": "incarnation-a",
            "expected_occupant": "occupant-a",
            "action": {
                "kind": "submit_text",
                "text": (
                    "Return your final answer as a single bare JSON value (no code fences, "
                    'no prose) matching this schema hint: {"type":"object"}\n\nreview this'
                ),
            },
            "require_absent": True,
        }
    ]


async def test_second_provider_send_is_refused_while_first_job_is_running(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = _target(daemon)
    _online(monkeypatch, daemon)
    callback_count = 0

    async def accepted(_provider, generation, _method, params):
        nonlocal callback_count
        callback_count += 1
        return {
            "operation_id": params["operation_id"],
            "provider_generation": generation,
            "terminal_id": params["terminal_id"],
            "terminal_incarnation": params["terminal_incarnation"],
            "delivery": "accepted",
        }

    monkeypatch.setattr(daemon.terminal_service.connections, "request", accepted)
    first = await controls_send(
        daemon,
        _context(),
        {"participant_id": participant_id, "prompt": "first"},
        idempotency_key="provider-send-first",
    )
    await _settle(daemon)
    assert daemon.operation_service.get(first["operation_id"]).state == "succeeded"

    second = await controls_send(
        daemon,
        _context(),
        {"participant_id": participant_id, "prompt": "second"},
        idempotency_key="provider-send-second",
    )
    await _settle(daemon)

    refused = daemon.operation_service.get(second["operation_id"])
    assert refused.state == PublicOperationState.FAILED.value
    assert refused.error is not None and refused.error["code"] == "busy"
    assert callback_count == 1


async def test_public_send_acceptance_atomically_persists_job_control_and_operation(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = _target(daemon)
    _online(monkeypatch, daemon)
    started: list[str] = []

    def suppress_start(operation_id, *, dispatch, side_effect):
        del dispatch, side_effect
        started.append(operation_id)

    monkeypatch.setattr(daemon.operation_service, "start", suppress_start)
    before = daemon.store.journal.current_sequence()
    params = {"participant_id": participant_id, "prompt": "persist first"}

    accepted = await controls_send(
        daemon,
        _context(),
        params,
        idempotency_key="atomic-public-send",
    )
    duplicate = await controls_send(
        daemon,
        _context(),
        params,
        idempotency_key="atomic-public-send",
    )

    operation = daemon.operation_service.get(accepted["operation_id"])
    control = daemon.store.get_control_operation(operation.control_operation_id)
    job = daemon.store.get_job(operation.job_handle)
    assert duplicate == accepted
    assert accepted["job_handle"] == operation.job_handle
    assert operation.state == PublicOperationState.ACCEPTED.value
    assert operation.phase == "control_reserved"
    assert control is not None and control.delivery_phase is ControlDeliveryPhase.RESERVED
    assert job.state == JobState.RUNNING
    assert started == [operation.operation_id]
    groups = daemon.store.journal.groups_after(before, limit=10)
    group = next(
        item
        for item in groups
        if any(event.entity_id == operation.operation_id for event in item.events)
    )
    assert [event.kind for event in group.events] == [
        "job.updated",
        "participant.controls_changed",
        "operation.updated",
    ]
    assert (
        daemon.store.conn.execute(
            select(orchestration_events.c.transaction_id).where(
                orchestration_events.c.ending_sequence == group.ending_sequence
            )
        )
        .scalars()
        .all()
        == [group.transaction_id] * 3
    )


async def test_provider_unknown_keeps_barrier_and_queued_followup(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = _target(daemon)
    _online(monkeypatch, daemon)
    callback_count = 0

    async def unknown(_provider, _generation, _method, _params):
        nonlocal callback_count
        callback_count += 1
        raise CallbackOutcomeUnknown("provider-a", "callback-a", "response_lost")

    monkeypatch.setattr(daemon.terminal_service.connections, "request", unknown)
    accepted = await controls_send(
        daemon,
        _context(),
        {"participant_id": participant_id, "prompt": "uncertain"},
        idempotency_key="provider-send-unknown",
    )
    await _settle(daemon)
    operation = daemon.operation_service.get(accepted["operation_id"])
    assert operation.state == PublicOperationState.UNCERTAIN.value
    assert daemon.store.has_execution_barrier(participant_id)
    control = daemon.store.get_control_operation(operation.control_operation_id)
    assert control is not None
    assert control.transport is ControlTransport.PROVIDER_TERMINAL
    assert daemon.store.unresolved_prompt_delivery_operations(participant_id) == []
    reconciled = await operations_reconcile(
        daemon,
        _context(),
        {"operation_id": operation.operation_id},
    )
    assert reconciled["state"] == PublicOperationState.UNCERTAIN.value
    assert callback_count == 1

    second = await controls_send(
        daemon,
        _context(),
        {"participant_id": participant_id, "prompt": "must not cross the barrier"},
        idempotency_key="provider-send-after-unknown",
    )
    await _settle(daemon)
    refused = daemon.operation_service.get(second["operation_id"])
    assert refused.state == PublicOperationState.FAILED.value
    assert refused.error is not None and refused.error["code"] == "busy"
    assert callback_count == 1

    queued = await controls_queue_followup(
        daemon,
        _context(),
        {"participant_id": participant_id, "prompt": "later"},
        idempotency_key="provider-queue-a",
    )
    control = await _control_for_public_operation(daemon, queued["operation_id"])
    await asyncio.sleep(0)
    queued_operation = daemon.operation_service.get(queued["operation_id"])
    assert queued_operation.state == PublicOperationState.RUNNING.value
    assert control is not None and control.delivery_phase.value == "queued"
    assert control.transport is ControlTransport.PROVIDER_TERMINAL
    await daemon.controls.reconcile_ambiguous_delivery(participant_id, now_ts=now() + 10_000)
    assert daemon.store.get_job(operation.job_handle).state == JobState.RUNNING
    assert daemon.store.has_execution_barrier(participant_id)
    assert daemon.store.get_control_operation(control.operation_id).delivery_phase.value == "queued"

    assert daemon.controls.fail_undelivered_followups([participant_id]) == []
    preserved = daemon.store.get_control_operation(control.operation_id)
    assert preserved is not None
    assert preserved.delivery_phase is ControlDeliveryPhase.QUEUED
    assert preserved.transport is ControlTransport.PROVIDER_TERMINAL
    assert (
        preserved.provider_id,
        preserved.provider_generation,
        preserved.terminal_id,
        preserved.terminal_incarnation,
    ) == ("provider-a", 1, "terminal-a", "incarnation-a")
    monkeypatch.setattr(daemon.terminal_service.connections, "health", lambda *_: "offline")
    deferred = await daemon.controls.dispatch_queue(participant_id)
    assert deferred.deferred is True
    assert daemon.store.get_control_operation(control.operation_id).delivery_phase.value == "queued"


def test_public_error_normalization_bounds_and_discards_invalid_details() -> None:
    class InvalidError(Exception):
        code = "x" * 600

        def __init__(self, message: str) -> None:
            self.details = {"not_json": object(), "not_finite": float("nan")}
            super().__init__(message)

    error = _error(InvalidError("m" * 9_000))
    assert len(error["code"]) == 512
    assert len(error["message"]) == 8192
    assert "details" not in error


async def test_provider_queue_waits_for_accepted_job_to_finish(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = _target(daemon)
    _online(monkeypatch, daemon)
    requests: list[str] = []

    async def accepted(_provider, generation, method, params):
        requests.append(method)
        return {
            "operation_id": params["operation_id"],
            "provider_generation": generation,
            "terminal_id": params["terminal_id"],
            "terminal_incarnation": params["terminal_incarnation"],
            "delivery": "accepted",
        }

    monkeypatch.setattr(daemon.terminal_service.connections, "request", accepted)
    sent = await controls_send(
        daemon,
        _context(),
        {"participant_id": participant_id, "prompt": "start"},
        idempotency_key="provider-active-send",
    )
    await _settle(daemon)
    send_operation = daemon.operation_service.get(sent["operation_id"])
    queued = await controls_queue_followup(
        daemon,
        _context(),
        {"participant_id": participant_id, "prompt": "later"},
        idempotency_key="provider-active-queue",
    )
    control = await _control_for_public_operation(daemon, queued["operation_id"])
    queued_operation = daemon.operation_service.get(queued["operation_id"])
    assert control is not None and control.delivery_phase.value == "queued"
    queued_job = daemon.store.get_job(queued_operation.job_handle)
    assert queued_job.actor_client_id == "operator-a"
    assert requests == ["terminal.deliver"]

    daemon.jobs.finish(send_operation.job_handle, state=JobState.DONE)
    await daemon.controls.dispatch_queue(participant_id)
    await _settle(daemon)
    assert daemon.operation_service.get(queued["operation_id"]).state == "succeeded"
    assert requests == ["terminal.deliver", "terminal.deliver"]


async def test_queued_public_operation_waiter_is_owned_and_drained_on_close(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = _target(daemon)
    _online(monkeypatch, daemon)

    async def accepted(_provider, generation, _method, params):
        return {
            "operation_id": params["operation_id"],
            "provider_generation": generation,
            "terminal_id": params["terminal_id"],
            "terminal_incarnation": params["terminal_incarnation"],
            "delivery": "accepted",
        }

    monkeypatch.setattr(daemon.terminal_service.connections, "request", accepted)
    sent = await controls_send(
        daemon,
        _context(),
        {"participant_id": participant_id, "prompt": "active"},
        idempotency_key="provider-owned-send",
    )
    await _settle(daemon)
    queued = await controls_queue_followup(
        daemon,
        _context(),
        {"participant_id": participant_id, "prompt": "held"},
        idempotency_key="provider-owned-queue",
    )
    await _control_for_public_operation(daemon, queued["operation_id"])
    await asyncio.sleep(0)
    assert daemon.operation_service.get(queued["operation_id"]).state == "running"
    assert daemon.operation_service.owned_tasks

    await daemon.operation_service.aclose()

    assert daemon.operation_service.owned_tasks == ()
    assert daemon.operation_service.get(queued["operation_id"]).state == "uncertain"
    assert (
        daemon.store.get_job(daemon.operation_service.get(sent["operation_id"]).job_handle).state
        == JobState.RUNNING
    )
    control = daemon.store.get_control_operation(
        daemon.operation_service.get(queued["operation_id"]).control_operation_id
    )
    assert control is not None and control.delivery_phase is ControlDeliveryPhase.QUEUED


async def test_provider_rejection_is_definitive_and_provider_settings_stay_unsupported(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = _target(daemon)
    _online(monkeypatch, daemon)
    methods: list[str] = []

    async def rejected(_provider, generation, method, params):
        methods.append(method)
        return {
            "operation_id": params["operation_id"],
            "provider_generation": generation,
            "terminal_id": params["terminal_id"],
            "terminal_incarnation": params["terminal_incarnation"],
            "delivery": "rejected",
            "error": {"code": "provider_busy", "message": "terminal refused input"},
        }

    monkeypatch.setattr(daemon.terminal_service.connections, "request", rejected)
    sent = await controls_send(
        daemon,
        _context(),
        {"participant_id": participant_id, "prompt": "refuse"},
        idempotency_key="provider-send-rejected",
    )
    await _settle(daemon)
    operation = daemon.operation_service.get(sent["operation_id"])
    assert operation.state == PublicOperationState.FAILED.value
    assert operation.control_operation_id is not None
    assert daemon.store.has_execution_barrier(participant_id) is False
    assert daemon.store.get_job(operation.job_handle).state == JobState.CRASHED

    settings = await controls_settings_update(
        daemon,
        _context(),
        {"participant_id": participant_id, "model": "gpt-5"},
        idempotency_key="provider-settings-refused",
    )
    _accepted("frontend.controls.settings.update", settings)
    await _settle(daemon)
    settings_operation = daemon.operation_service.get(settings["operation_id"])
    assert settings_operation.state == PublicOperationState.FAILED.value
    assert settings_operation.control_operation_id is None
    assert methods == ["terminal.deliver"]


def test_native_route_is_not_replaced_by_terminal_binding(daemon) -> None:
    participant_id = _target(daemon)
    resolver = ControlRouteResolver(
        store=daemon.store,
        runtime_for=lambda _participant_id: object(),
        provider_health=lambda _provider_id, _generation: "online",
    )
    route = resolver.resolve(participant_id, RuntimeCapability.SEND)
    assert route.is_native
    assert not route.is_provider


def test_historical_pane_without_binding_is_not_a_control_route(daemon) -> None:
    participant = daemon.registry.register(harness="codex", pane=None, cwd=None)
    participant.tier = Tier.SPAWNED
    participant.tmux_pane = "%legacy"
    daemon.store.upsert_participant(participant)
    resolver = ControlRouteResolver(store=daemon.store, runtime_for=lambda _participant_id: None)

    route = resolver.resolve(participant.id, RuntimeCapability.SEND)

    assert route.transport is None
    assert not route.route_available
    assert not route.is_provider
    assert not route.is_legacy


async def test_provider_steer_and_interrupt_use_existing_job_and_exact_fence(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = _target(daemon)
    _online(monkeypatch, daemon)
    methods: list[str] = []

    async def accepted(_provider, generation, method, params):
        methods.append(method)
        return {
            "operation_id": params["operation_id"],
            "provider_generation": generation,
            "terminal_id": params["terminal_id"],
            "terminal_incarnation": params["terminal_incarnation"],
            "delivery": "accepted",
        }

    monkeypatch.setattr(daemon.terminal_service.connections, "request", accepted)
    sent = await controls_send(
        daemon,
        _context(),
        {"participant_id": participant_id, "prompt": "start"},
        idempotency_key="provider-send-running",
    )
    await _settle(daemon)
    send_operation = daemon.operation_service.get(sent["operation_id"])
    steered = await controls_steer(
        daemon,
        _context(),
        {"participant_id": participant_id, "prompt": "adjust"},
        idempotency_key="provider-steer-a",
    )
    _accepted("frontend.controls.steer", steered)
    steer_reserved = daemon.operation_service.get(steered["operation_id"])
    assert steer_reserved.state == PublicOperationState.ACCEPTED.value
    assert steer_reserved.job_handle == send_operation.job_handle
    assert daemon.store.get_control_operation(steer_reserved.control_operation_id) is not None
    await _settle(daemon)
    steer_operation = daemon.operation_service.get(steered["operation_id"])
    assert steer_operation.job_handle == send_operation.job_handle
    interrupted = await controls_interrupt(
        daemon,
        _context(),
        {"participant_id": participant_id},
        idempotency_key="provider-interrupt-a",
    )
    _accepted("frontend.controls.interrupt", interrupted)
    interrupt_reserved = daemon.operation_service.get(interrupted["operation_id"])
    assert interrupt_reserved.state == PublicOperationState.ACCEPTED.value
    assert interrupt_reserved.control_operation_id is not None
    assert interrupt_reserved.job_handle is None
    await _settle(daemon)
    assert daemon.operation_service.get(interrupted["operation_id"]).state == "succeeded"
    daemon.jobs.finish(send_operation.job_handle, state=JobState.DONE)
    queued = await controls_queue_followup(
        daemon,
        _context(),
        {"participant_id": participant_id, "prompt": "next"},
        idempotency_key="provider-queue-dispatch",
    )
    _accepted("frontend.controls.queue_followup", queued)
    queue_reserved = daemon.operation_service.get(queued["operation_id"])
    assert queue_reserved.state == PublicOperationState.ACCEPTED.value
    assert queue_reserved.control_operation_id is not None
    assert queued["job_handle"] == queue_reserved.job_handle
    await _settle(daemon)
    assert daemon.operation_service.get(queued["operation_id"]).state == "succeeded"
    assert methods == [
        "terminal.deliver",
        "terminal.deliver",
        "terminal.interrupt",
        "terminal.deliver",
    ]


@pytest.mark.parametrize("changed", ["generation", "incarnation"])
async def test_provider_identity_change_refuses_without_dispatch(
    daemon, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    participant_id = _target(daemon)
    _online(monkeypatch, daemon)
    called = False

    async def request(*_args):
        nonlocal called
        called = True
        raise AssertionError("stale public dispatch must not reach the provider")

    monkeypatch.setattr(daemon.terminal_service.connections, "request", request)
    accepted = await controls_send(
        daemon,
        _context(),
        {"participant_id": participant_id, "prompt": "fenced"},
        idempotency_key="provider-send-fenced",
    )
    with daemon.store.write_unit() as unit:
        if changed == "generation":
            assert daemon.store.terminal_bindings.restore_generation(
                participant_id,
                previous_generation=1,
                provider_generation=2,
                report_revision=2,
                health="healthy",
                updated_at=now(),
                connection=unit.connection,
            )
        else:
            unit.connection.execute(
                update(terminal_bindings)
                .where(terminal_bindings.c.participant_id == participant_id)
                .values(terminal_incarnation="incarnation-b", updated_at=now())
            )
    await _settle(daemon)
    assert not called
    assert daemon.operation_service.get(accepted["operation_id"]).state == "failed"


async def test_controls_projection_is_schema_valid_and_unknown_presence_blocks(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = _target(daemon)
    _online(monkeypatch, daemon)
    daemon.presence = UnknownPresence()
    dispatched = False

    async def request(*_args):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("unknown presence must block provider delivery")

    monkeypatch.setattr(daemon.terminal_service.connections, "request", request)
    result = await controls_get(daemon, _context(), {"participant_id": participant_id})
    validator_for(METHOD_CATALOG["frontend.controls.get"].result_schema_id).validate(result)
    assert result["actions"]["send"]["route_available"] is True
    assert result["actions"]["send"]["admissible"] is False
    assert result["actions"]["settings_update"]["supported"] is False
    assert result["wiring"] == "provider"
    assert result["health"] == {"connection": "online", "diagnostics": []}
    assert result["settings"] is None
    assert result["active_turn"] is None
    assert result["queued"] == []
    assert result["human_presence"]["state"] == "unknown"
    accepted = await controls_send(
        daemon,
        _context(),
        {"participant_id": participant_id, "prompt": "blocked"},
        idempotency_key="provider-presence-blocked",
    )
    await _settle(daemon)
    operation = daemon.operation_service.get(accepted["operation_id"])
    assert operation.state == PublicOperationState.FAILED.value
    control = daemon.store.get_control_operation(operation.control_operation_id)
    assert control is not None
    assert control.delivery_phase is ControlDeliveryPhase.SETTLED
    assert control.delivery_result.value == "rejected"
    assert daemon.store.get_job(operation.job_handle).state == JobState.CRASHED
    assert not dispatched


async def test_presence_transition_publishes_one_controls_change_and_stable_poll_none(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = _target(daemon)
    _online(monkeypatch, daemon)
    cursor = daemon.store.journal.current_sequence()

    await daemon.presence.refresh()

    changed = [
        event
        for group in daemon.store.journal.groups_after(cursor, limit=20)
        for event in group.events
        if event.kind == "participant.controls_changed" and event.entity_id == participant_id
    ]
    assert len(changed) == 1
    assert changed[0].payload["presence"] == "absent"

    cursor = daemon.store.journal.current_sequence()
    await daemon.presence.refresh()
    unchanged = [
        event
        for group in daemon.store.journal.groups_after(cursor, limit=20)
        for event in group.events
        if event.kind == "participant.controls_changed" and event.entity_id == participant_id
    ]
    assert unchanged == []


@pytest.mark.parametrize(
    ("transport", "identity"),
    [
        (
            ControlTransport.PROVIDER_TERMINAL,
            {
                "provider_id": "provider-a",
                "provider_generation": 1,
                "terminal_id": "terminal-a",
                "terminal_incarnation": "incarnation-a",
            },
        ),
        (
            ControlTransport.NATIVE_RUNTIME,
            {"backend_generation": 4, "native_session_id": "native-session-a"},
        ),
    ],
)
async def test_production_reconciler_settles_exact_durable_control_evidence(
    daemon,
    monkeypatch: pytest.MonkeyPatch,
    transport: ControlTransport,
    identity: dict[str, object],
) -> None:
    participant_id = daemon.registry.register(harness="codex", pane=None, cwd=None).id
    operation_id = f"reconcile-{transport.value}"
    control_id = f"{operation_id}:control"
    timestamp = now()
    with daemon.store.write_unit() as unit:
        daemon.store.reserve_control_operation(
            ControlOperation(
                operation_id=control_id,
                participant_id=participant_id,
                kind=ControlKind.INTERRUPT,
                transport=transport,
                delivery_phase=ControlDeliveryPhase.SETTLED,
                delivery_result=DeliveryResult.ACCEPTED,
                created_at=timestamp,
                updated_at=timestamp,
                **identity,
            ),
            connection=unit.connection,
        )
        daemon.store.operations.create(
            PublicOperationRecord(
                operation_id=operation_id,
                kind="controls.interrupt",
                actor_client_id="operator-a",
                actor_participant_id=None,
                target_ids=(participant_id,),
                state=PublicOperationState.UNCERTAIN.value,
                phase="delivery_unknown",
                control_operation_id=control_id,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )

    async def forbidden_dispatch(*_args, **_kwargs):
        raise AssertionError("reconciliation must never dispatch")

    monkeypatch.setattr(daemon.terminal_service.connections, "request", forbidden_dispatch)
    reconciled = await operations_reconcile(
        daemon,
        _context(),
        {"operation_id": operation_id},
    )

    assert reconciled["state"] == PublicOperationState.SUCCEEDED.value
    assert reconciled["phase"] == "delivery_evidence_reconciled"
    assert reconciled["result"] == {"delivery": "accepted"}


async def test_public_native_settings_reservation_precedes_detached_dispatch(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = daemon.registry.register(harness="codex", pane=None, cwd=None).id
    daemon.store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id=participant_id,
            harness="codex",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=7,
            lifecycle=RuntimeLifecyclePhase.ACTIVE,
            native_session_id="native-session-settings",
            created_at=now(),
            updated_at=now(),
        )
    )
    runtime, _state = await _install_native_runtime(
        daemon,
        participant_id,
        generation=7,
        session_id="native-session-settings",
    )
    assert daemon.runtime_manager.record_snapshot(participant_id, runtime, await runtime.snapshot())
    started: list[str] = []

    def suppress_start(operation_id, *, dispatch, side_effect):
        del dispatch, side_effect
        started.append(operation_id)

    monkeypatch.setattr(daemon.operation_service, "start", suppress_start)
    accepted = await controls_settings_update(
        daemon,
        _context(),
        {"participant_id": participant_id, "model": "gpt-5"},
        idempotency_key="atomic-native-settings",
    )

    operation = daemon.operation_service.get(accepted["operation_id"])
    control = daemon.store.get_control_operation(operation.control_operation_id)
    assert operation.state == PublicOperationState.ACCEPTED.value
    assert control is not None
    assert control.transport is ControlTransport.NATIVE_RUNTIME
    assert control.backend_generation == 7
    assert control.native_session_id == "native-session-settings"
    assert started == [operation.operation_id]


async def test_cached_native_route_is_consistent_across_public_read_and_admission(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant = daemon.registry.register(harness="codex", pane=None, cwd=None)
    participant_id = participant.id
    daemon.presence = AbsentPresence()
    daemon.store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id=participant_id,
            harness="codex",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=9,
            lifecycle=RuntimeLifecyclePhase.ACTIVE,
            native_session_id="native-session-consistent",
            created_at=now(),
            updated_at=now(),
        )
    )
    runtime, _state = await _install_native_runtime(
        daemon,
        participant_id,
        generation=9,
        session_id="native-session-consistent",
    )

    unavailable = daemon.controls.route_for(participant_id, RuntimeCapability.SETTINGS_UPDATE)
    assert unavailable.route_available is False
    before, _cursor = daemon.store.operations.list_page(cursor=None, limit=100)
    with pytest.raises(StaleTarget, match=r"native runtime route.*unavailable"):
        await controls_settings_update(
            daemon,
            _context(),
            {"participant_id": participant_id, "model": "gpt-5"},
            idempotency_key="native-settings-before-snapshot",
        )
    after, _cursor = daemon.store.operations.list_page(cursor=None, limit=100)
    assert after == before

    participant_value = await participant_to_wire(daemon, participant)
    assert participant_value["native_route"]["health"] == "connected"
    assert participant_value["actions"]["settings_update"]["route_available"] is True
    cached = daemon.runtime_manager.cached_native_route(
        participant_id,
        backend_generation=9,
        native_session_id="native-session-consistent",
    )
    assert cached is not None and cached["health"] == "connected"
    assert daemon.controls.route_for(
        participant_id, RuntimeCapability.SETTINGS_UPDATE
    ).route_available

    controls_value = await controls_get(daemon, _context(), {"participant_id": participant_id})
    assert controls_value["actions"]["settings_update"]["route_available"] is True
    snapshot = daemon.state_service.snapshot("native-route-client", page_size=500)
    snapshot_value = next(
        item for item in snapshot["participants"] if item["participant_id"] == participant_id
    )
    assert snapshot_value["native_route"] == participant_value["native_route"]
    assert snapshot_value["actions"]["settings_update"]["route_available"] is True

    started: list[str] = []

    def suppress_start(operation_id, *, dispatch, side_effect):
        del dispatch, side_effect
        started.append(operation_id)

    monkeypatch.setattr(daemon.operation_service, "start", suppress_start)
    admitted = await controls_settings_update(
        daemon,
        _context(),
        {"participant_id": participant_id, "model": "gpt-5"},
        idempotency_key="native-settings-after-snapshot",
    )
    assert started == [admitted["operation_id"]]
    control = daemon.store.get_control_operation(
        daemon.operation_service.get(admitted["operation_id"]).control_operation_id
    )
    assert control is not None
    assert (control.backend_generation, control.native_session_id) == (
        9,
        "native-session-consistent",
    )
    assert daemon.runtime_manager.record_snapshot(participant_id, runtime, await runtime.snapshot())


async def test_native_capability_projection_is_consistent_across_public_reads(daemon) -> None:
    participant = daemon.registry.register(harness="codex", pane=None, cwd=None)
    daemon.presence = AbsentPresence()
    daemon.store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id=participant.id,
            harness="codex",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=12,
            lifecycle=RuntimeLifecyclePhase.ACTIVE,
            native_session_id="native-session-gated",
            created_at=now(),
            updated_at=now(),
        )
    )
    _runtime, state = await _install_native_runtime(
        daemon,
        participant.id,
        generation=12,
        session_id="native-session-gated",
    )
    state.unavailable[RuntimeCapability.SETTINGS_UPDATE] = (
        CapabilityUnavailableReason.GATED_BY_BACKEND
    )

    before_controls = await controls_get(daemon, _context(), {"participant_id": participant.id})
    before_snapshot = daemon.state_service.snapshot("native-capability-before", page_size=500)
    before_value = next(
        item for item in before_snapshot["participants"] if item["participant_id"] == participant.id
    )
    assert before_controls["actions"]["send"]["supported"] is False
    assert before_controls["actions"]["send"]["reason"] == "not_determined"
    assert before_controls["health"]["connection"] == "disconnected"
    assert before_controls["settings"] is None
    assert before_value["actions"]["send"]["supported"] is False
    assert before_value["actions"]["send"]["reason"] == "not_determined"

    participant_value = await participant_to_wire(daemon, participant)
    controls_value = await controls_get(daemon, _context(), {"participant_id": participant.id})
    snapshot = daemon.state_service.snapshot("native-capability-client", page_size=500)
    snapshot_value = next(
        item for item in snapshot["participants"] if item["participant_id"] == participant.id
    )

    expected = {
        "supported": False,
        "route_available": True,
        "admissible": False,
        "reason": "gated_by_backend",
        "detail": None,
    }
    assert participant_value["actions"]["settings_update"] == expected
    assert controls_value["actions"]["settings_update"] == expected
    assert snapshot_value["actions"]["settings_update"] == expected
    assert controls_value["health"] == {"connection": "connected", "diagnostics": []}
    assert controls_value["settings"] == {"model": None, "reasoning_effort": None}
    assert set(participant_value["native_route"]) == {
        "backend_generation",
        "native_session_id",
        "health",
    }


async def test_native_busy_admission_is_consistent_across_public_reads(daemon) -> None:
    participant = daemon.registry.register(harness="codex", pane=None, cwd=None)
    daemon.presence = AbsentPresence()
    daemon.store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id=participant.id,
            harness="codex",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=14,
            lifecycle=RuntimeLifecyclePhase.ACTIVE,
            native_session_id="native-session-busy",
            created_at=now(),
            updated_at=now(),
        )
    )
    _runtime, state = await _install_native_runtime(
        daemon,
        participant.id,
        generation=14,
        session_id="native-session-busy",
    )
    state.native_turn_id = "external-turn"

    participant_value = await participant_to_wire(daemon, participant)
    controls_value = await controls_get(daemon, _context(), {"participant_id": participant.id})
    snapshot = daemon.state_service.snapshot("native-busy-client", page_size=500)
    snapshot_value = next(
        item for item in snapshot["participants"] if item["participant_id"] == participant.id
    )

    for value in (participant_value, controls_value, snapshot_value):
        assert value["actions"]["send"]["admissible"] is False
        assert value["actions"]["send"]["reason"] == "busy"
        assert value["actions"]["settings_update"]["admissible"] is False
        assert value["actions"]["settings_update"]["reason"] == "busy"
        assert value["actions"]["steer"]["admissible"] is False
        assert value["actions"]["steer"]["reason"] == "stale_target"


async def test_native_settings_admission_requires_a_mutable_field(daemon) -> None:
    participant = daemon.registry.register(harness="codex", pane=None, cwd=None)
    daemon.presence = AbsentPresence()
    daemon.store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id=participant.id,
            harness="codex",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=15,
            lifecycle=RuntimeLifecyclePhase.ACTIVE,
            native_session_id="native-session-fixed-settings",
            created_at=now(),
            updated_at=now(),
        )
    )
    _runtime, state = await _install_native_runtime(
        daemon,
        participant.id,
        generation=15,
        session_id="native-session-fixed-settings",
    )
    state.supported_settings.clear()

    participant_value = await participant_to_wire(daemon, participant)
    controls_value = await controls_get(daemon, _context(), {"participant_id": participant.id})
    snapshot = daemon.state_service.snapshot("native-settings-client", page_size=500)
    snapshot_value = next(
        item for item in snapshot["participants"] if item["participant_id"] == participant.id
    )

    for value in (participant_value, controls_value, snapshot_value):
        assert value["actions"]["settings_update"]["supported"] is True
        assert value["actions"]["settings_update"]["admissible"] is False
        assert value["actions"]["settings_update"]["reason"] == "unsupported"


async def test_unknown_presence_detail_is_consistent_across_public_reads(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = _target(daemon)
    _online(monkeypatch, daemon)
    daemon.presence = UnknownPresence()
    participant = daemon.registry.get(participant_id)

    participant_value = await participant_to_wire(daemon, participant)
    controls_value = await controls_get(daemon, _context(), {"participant_id": participant.id})
    snapshot = daemon.state_service.snapshot("unknown-presence-client", page_size=500)
    snapshot_value = next(
        item for item in snapshot["participants"] if item["participant_id"] == participant.id
    )

    for value in (participant_value, controls_value, snapshot_value):
        assert value["actions"]["send"]["reason"] == "presence_unknown"
        assert value["actions"]["send"]["detail"] == "test double: unknown focus"


async def test_transcript_trust_is_consistent_across_public_reads(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = _target(daemon)
    _online(monkeypatch, daemon)
    daemon.presence = AbsentPresence()
    participant = daemon.registry.get(participant_id)
    participant.tier = Tier.ADOPTED
    daemon.store.upsert_participant(participant)

    values = await _public_action_views(daemon, participant, actor="untrusted-transcript")
    for value in values:
        assert value["actions"]["send"]["admissible"] is False
        assert value["actions"]["send"]["reason"] == "transcript_untrusted"

    daemon.observer.mark_transcript_identity_lost(participant.id, "test identity loss")
    values = await _public_action_views(daemon, participant, actor="lost-transcript")
    for value in values:
        assert value["actions"]["send"]["admissible"] is False
        assert value["actions"]["send"]["reason"] == "transcript_identity_lost"


async def test_empty_setting_allowlists_are_consistent_across_public_reads(daemon) -> None:
    participant = daemon.registry.register(harness="codex", pane=None, cwd=None)
    daemon.presence = AbsentPresence()
    daemon.config.models["codex"] = []
    daemon.config.reasoning["codex"] = []
    daemon.store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id=participant.id,
            harness="codex",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=16,
            lifecycle=RuntimeLifecyclePhase.ACTIVE,
            native_session_id="native-session-no-allowed-settings",
            created_at=now(),
            updated_at=now(),
        )
    )
    await _install_native_runtime(
        daemon,
        participant.id,
        generation=16,
        session_id="native-session-no-allowed-settings",
    )

    values = await _public_action_views(daemon, participant, actor="empty-setting-allowlists")
    for value in values:
        action = value["actions"]["settings_update"]
        assert action["supported"] is True
        assert action["admissible"] is False
        assert action["reason"] == "unsupported"
        assert action["detail"] == (
            "no runtime-supported setting field has configured allowable values"
        )

    daemon.config.models["codex"] = ["gpt-5"]
    values = await _public_action_views(daemon, participant, actor="allowed-model-setting")
    for value in values:
        action = value["actions"]["settings_update"]
        assert action["admissible"] is True
        assert action["reason"] is None


async def test_presence_snapshot_failures_are_consistent_across_public_reads(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingPresence:
        def snapshot(self, _participant_id: str):
            raise RuntimeError("provider read failed")

    participant_id = _target(daemon)
    _online(monkeypatch, daemon)
    daemon.presence = FailingPresence()
    participant = daemon.registry.get(participant_id)

    values = await _public_action_views(daemon, participant, actor="failed-presence")
    for value in values:
        assert value["actions"]["send"]["admissible"] is False
        assert value["actions"]["send"]["reason"] == "presence_unknown"
        assert value["actions"]["send"]["detail"] == "presence snapshot failed"


async def test_private_native_send_refuses_a_durable_session_mismatch(daemon) -> None:
    participant = daemon.registry.register(harness="codex", pane=None, cwd=None)
    daemon.presence = AbsentPresence()
    daemon.store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id=participant.id,
            harness="codex",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=13,
            lifecycle=RuntimeLifecyclePhase.ACTIVE,
            native_session_id="durable-session",
            created_at=now(),
            updated_at=now(),
        )
    )
    _runtime, state = await _install_native_runtime(
        daemon,
        participant.id,
        generation=13,
        session_id="different-live-session",
    )

    with pytest.raises(StaleTarget, match="exact native runtime route"):
        await daemon.controls.send(participant.id, caller_id="cli", prompt="identity mismatch")

    assert state.sent == []
    assert daemon.store.running_jobs_for_target(participant.id) == []


async def test_queued_native_followup_never_crosses_its_reserved_session(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant = daemon.registry.register(harness="codex", pane=None, cwd=None)
    daemon.presence = AbsentPresence()
    daemon.store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id=participant.id,
            harness="codex",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=14,
            lifecycle=RuntimeLifecyclePhase.ACTIVE,
            native_session_id="session-a",
            created_at=now(),
            updated_at=now(),
        )
    )
    _runtime, state = await _install_native_runtime(
        daemon, participant.id, generation=14, session_id="session-a"
    )
    monkeypatch.setattr(daemon.controls, "schedule_dispatch", lambda _participant_id: None)
    queued = await daemon.controls.queue_followup(
        participant.id,
        caller_id="cli",
        prompt="must stay in session A",
    )
    (reserved,) = daemon.store.queued_control_operations(participant.id)
    assert reserved.native_session_id == "session-a"

    assert daemon.store.bind_runtime_identity(
        participant.id,
        backend_generation=14,
        native_session_id="session-b",
        updated_at=now(),
    )
    state.native_session_id = "session-b"
    outcome = await daemon.controls.dispatch_queue(participant.id)

    assert outcome.failed == ((queued.handle, "stale_target"),)
    assert state.sent == []
    operation = daemon.store.get_control_operation(reserved.operation_id)
    assert operation is not None and operation.native_session_id == "session-a"


async def test_public_admission_fences_the_exact_cached_native_session(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = daemon.registry.register(harness="codex", pane=None, cwd=None).id
    daemon.presence = AbsentPresence()
    daemon.store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id=participant_id,
            harness="codex",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=10,
            lifecycle=RuntimeLifecyclePhase.ACTIVE,
            native_session_id=None,
            created_at=now(),
            updated_at=now(),
        )
    )
    runtime, state = await _install_native_runtime(
        daemon,
        participant_id,
        generation=10,
        session_id="native-session-a",
    )
    assert daemon.runtime_manager.record_snapshot(participant_id, runtime, await runtime.snapshot())
    monkeypatch.setattr(
        daemon.operation_service,
        "start",
        lambda _operation_id, *, dispatch, side_effect: None,
    )

    with pytest.raises(StaleTarget, match=r"native runtime route.*unavailable"):
        await controls_settings_update(
            daemon,
            _context(),
            {"participant_id": participant_id, "model": "gpt-5"},
            idempotency_key="native-session-before-durable-bind",
        )
    assert (
        daemon.runtime_manager.cached_native_route(
            participant_id,
            backend_generation=10,
            native_session_id=None,
        )
        is None
    )

    assert daemon.store.bind_runtime_identity(
        participant_id,
        backend_generation=10,
        native_session_id="native-session-a",
        updated_at=now(),
    )
    admitted = await controls_settings_update(
        daemon,
        _context(),
        {"participant_id": participant_id, "model": "gpt-5"},
        idempotency_key="native-session-cache-fence",
    )
    operation = daemon.operation_service.get(admitted["operation_id"])
    control = daemon.store.get_control_operation(operation.control_operation_id)
    assert control is not None
    assert (control.backend_generation, control.native_session_id) == (
        10,
        "native-session-a",
    )

    state.native_session_id = "native-session-b"
    with pytest.raises(StaleTarget, match="native session changed"):
        daemon.controls._require_reserved_native_identity(control, await runtime.snapshot())


async def test_participant_read_rejects_a_cached_native_session_mismatch(daemon) -> None:
    participant = daemon.registry.register(harness="codex", pane=None, cwd=None)
    daemon.presence = AbsentPresence()
    daemon.store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id=participant.id,
            harness="codex",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=11,
            lifecycle=RuntimeLifecyclePhase.ACTIVE,
            native_session_id="durable-session",
            created_at=now(),
            updated_at=now(),
        )
    )
    await _install_native_runtime(
        daemon,
        participant.id,
        generation=11,
        session_id="different-session",
    )

    projected = await participant_to_wire(daemon, participant)
    assert projected["native_route"] == {
        "backend_generation": 11,
        "native_session_id": "durable-session",
        "health": "disconnected",
    }
    assert projected["actions"]["settings_update"]["route_available"] is False
    assert (
        daemon.controls.route_for(participant.id, RuntimeCapability.SETTINGS_UPDATE).route_available
        is False
    )
    snapshot = daemon.state_service.snapshot("mismatched-native-session", page_size=500)
    snapshot_value = next(
        item for item in snapshot["participants"] if item["participant_id"] == participant.id
    )
    assert snapshot_value["native_route"] == projected["native_route"]
    assert snapshot_value["actions"] == projected["actions"]


async def test_production_reconciler_accepts_exact_native_terminal_evidence(daemon) -> None:
    participant_id = daemon.registry.register(harness="codex", pane=None, cwd=None).id
    operation_id = "reconcile-native-evidence"
    control_id = f"{operation_id}:control"
    timestamp = now()
    with daemon.store.write_unit() as unit:
        daemon.store.reserve_control_operation(
            ControlOperation(
                operation_id=control_id,
                participant_id=participant_id,
                kind=ControlKind.SEND,
                transport=ControlTransport.NATIVE_RUNTIME,
                delivery_phase=ControlDeliveryPhase.SETTLED,
                delivery_result=DeliveryResult.UNKNOWN,
                job_handle=f"{participant_id}#1",
                backend_generation=4,
                native_session_id="native-session-evidence",
                native_turn_id="native-turn-evidence",
                execution_barrier=True,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
        assert daemon.store.record_native_terminal_evidence(
            NativeTerminalEvidence(
                participant_id=participant_id,
                backend_generation=4,
                native_session_id="native-session-evidence",
                native_turn_id="native-turn-evidence",
                terminal=NativeTurnTerminal.COMPLETED,
                result="done",
                completeness=ResultCompleteness.COMPLETE,
                provenance=ResultProvenance.NATIVE_EVIDENCE,
                recorded_at=timestamp,
            ),
            connection=unit.connection,
        )
        daemon.store.operations.create(
            PublicOperationRecord(
                operation_id=operation_id,
                kind="controls.send",
                actor_client_id="operator-a",
                actor_participant_id=None,
                target_ids=(participant_id,),
                state=PublicOperationState.UNCERTAIN.value,
                phase="delivery_unknown",
                control_operation_id=control_id,
                job_handle=f"{participant_id}#1",
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )

    reconciled = await operations_reconcile(
        daemon,
        _context(),
        {"operation_id": operation_id},
    )

    assert reconciled["state"] == PublicOperationState.SUCCEEDED.value
    assert reconciled["phase"] == "native_evidence_reconciled"


async def test_production_reconciler_settles_proven_workspace_removal(daemon, tmp_path) -> None:
    operation_id = "reconcile-workspace"
    workspace_id = "workspace-reconciled"
    timestamp = now()
    with daemon.store.write_unit() as unit:
        daemon.store.workspaces.create(
            WorkspaceRecord(
                workspace_id=workspace_id,
                ownership_kind=WorkspaceOwnershipKind.THEATER.value,
                owner_id="operator-a",
                path=str(tmp_path / "removed"),
                state=WorkspaceState.REMOVED.value,
                deletion_operation_id="other-cleanup",
                cleanup_force=False,
                cleanup_delete_branch=False,
                cleanup_force_branch=False,
                cleanup_result={
                    "worktree_removed": True,
                    "branch_removed": False,
                    "branch_retained": True,
                    "errors": [],
                    "uncertain": False,
                },
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
        daemon.store.operations.create(
            PublicOperationRecord(
                operation_id=operation_id,
                kind="workspace_cleanup",
                actor_client_id="operator-a",
                actor_participant_id=None,
                target_ids=(workspace_id,),
                state=PublicOperationState.UNCERTAIN.value,
                phase="outcome_persistence_error",
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )

    unmatched = await operations_reconcile(
        daemon,
        _context(),
        {"operation_id": operation_id},
    )
    assert unmatched["state"] == PublicOperationState.UNCERTAIN.value

    with daemon.store.write_unit() as unit:
        unit.connection.execute(
            update(workspaces)
            .where(workspaces.c.workspace_id == workspace_id)
            .values(deletion_operation_id=operation_id)
        )
    reconciled = await operations_reconcile(
        daemon,
        _context(),
        {"operation_id": operation_id},
    )

    assert reconciled["state"] == PublicOperationState.SUCCEEDED.value
    assert reconciled["phase"] == "workspace_cleanup_evidence_reconciled"
    assert reconciled["result"]["worktree_removed"] is True
    assert reconciled["result"]["workspace"]["state"] == WorkspaceState.REMOVED.value


async def test_production_reconciler_requires_structured_workspace_cleanup_evidence(
    daemon, tmp_path
) -> None:
    operation_id = "reconcile-workspace-without-evidence"
    workspace_id = "workspace-without-evidence"
    timestamp = now()
    with daemon.store.write_unit() as unit:
        daemon.store.workspaces.create(
            WorkspaceRecord(
                workspace_id=workspace_id,
                ownership_kind=WorkspaceOwnershipKind.THEATER.value,
                owner_id="operator-a",
                path=str(tmp_path / "removed-without-evidence"),
                state=WorkspaceState.REMOVED.value,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
        daemon.store.operations.create(
            PublicOperationRecord(
                operation_id=operation_id,
                kind="workspace_cleanup",
                actor_client_id="operator-a",
                actor_participant_id=None,
                target_ids=(workspace_id,),
                state=PublicOperationState.UNCERTAIN.value,
                phase="outcome_persistence_error",
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )

    reconciled = await operations_reconcile(
        daemon,
        _context(),
        {"operation_id": operation_id},
    )

    assert reconciled["state"] == PublicOperationState.UNCERTAIN.value
    assert reconciled["phase"] == "outcome_persistence_error"


async def test_verified_provider_termination_releases_usage_but_retains_workspace(
    daemon, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    participant_id = _target(daemon)
    _online(monkeypatch, daemon)
    daemon.store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id=participant_id,
            harness="codex",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=9,
            lifecycle=RuntimeLifecyclePhase.ACTIVE,
            native_session_id="native-session-a",
            created_at=now(),
            updated_at=now(),
        )
    )
    participant = daemon.registry.get(participant_id)
    participant.workspace_id = "workspace-a"
    daemon.store.upsert_participant(participant)
    timestamp = now()
    with daemon.store.write_unit() as unit:
        daemon.store.workspaces.create(
            WorkspaceRecord(
                workspace_id="workspace-a",
                ownership_kind=WorkspaceOwnershipKind.BORROWED.value,
                owner_id="operator-a",
                path=str(tmp_path),
                state=WorkspaceState.ACTIVE.value,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
        assert daemon.store.workspaces.acquire_usage(
            WorkspaceUsageRecord(
                usage_id="usage-a",
                workspace_id="workspace-a",
                holder_kind=WorkspaceUsageHolderKind.PARTICIPANT.value,
                holder_id=participant_id,
                acquired_at=timestamp,
            ),
            connection=unit.connection,
        )

    async def terminate(_provider, generation, method, params):
        assert method == "terminal.terminate"
        return {
            "operation_id": params["operation_id"],
            "provider_generation": generation,
            "terminal_id": params["terminal_id"],
            "terminal_incarnation": params["terminal_incarnation"],
            "delivery": "accepted",
            "exit_confirmed": True,
        }

    monkeypatch.setattr(daemon.terminal_service.connections, "request", terminate)
    handle = f"{participant_id}#99"
    daemon.jobs.create(
        handle=handle,
        caller_id="cli",
        target_id=participant_id,
        kind="send",
    )

    async def backend_stopped(_daemon, checked_id, caller_id):
        assert checked_id == participant_id
        assert caller_id == "cli"
        assert daemon.store.get_job(handle).state == JobState.RUNNING
        assert daemon.store.workspaces.active_usages("workspace-a")

    monkeypatch.setattr(participant_rpc, "_require_verified_backend_stop", backend_stopped)
    accepted = await participants_terminate(
        daemon,
        _context(),
        {"participant_id": participant_id},
        idempotency_key="provider-terminate-a",
    )
    _accepted("frontend.participants.terminate", accepted)
    await _settle(daemon)
    operation = daemon.operation_service.get(accepted["operation_id"])
    assert operation.state == PublicOperationState.SUCCEEDED.value, operation.error
    dispatch = operation_to_wire(operation)["dispatch_identity"]
    assert isinstance(dispatch, dict)
    assert dispatch["terminal"]["provider_id"] == "provider-a"
    assert dispatch["terminal"]["terminal_incarnation"] == "incarnation-a"
    assert dispatch["backend_generation"] == 9
    assert dispatch["native_session_id"] == "native-session-a"
    assert daemon.registry.get(participant_id).status is Status.DEAD
    assert daemon.store.get_runtime_binding(participant_id) is None
    assert daemon.store.get_job(handle).state == JobState.KILLED
    assert daemon.store.workspaces.get("workspace-a") is not None
    assert daemon.store.workspaces.active_usages("workspace-a") == []


async def test_unverified_native_stop_retains_provider_participant_job_and_usage(
    daemon, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    participant_id = _target(daemon)
    _online(monkeypatch, daemon)
    participant = daemon.registry.get(participant_id)
    participant.workspace_id = "workspace-uncertain"
    daemon.store.upsert_participant(participant)
    timestamp = now()
    with daemon.store.write_unit() as unit:
        daemon.store.workspaces.create(
            WorkspaceRecord(
                workspace_id="workspace-uncertain",
                ownership_kind=WorkspaceOwnershipKind.BORROWED.value,
                owner_id="operator-a",
                path=str(tmp_path),
                state=WorkspaceState.ACTIVE.value,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
        assert daemon.store.workspaces.acquire_usage(
            WorkspaceUsageRecord(
                usage_id="usage-uncertain",
                workspace_id="workspace-uncertain",
                holder_kind=WorkspaceUsageHolderKind.PARTICIPANT.value,
                holder_id=participant_id,
                acquired_at=timestamp,
            ),
            connection=unit.connection,
        )
    handle = f"{participant_id}#100"
    daemon.jobs.create(handle=handle, caller_id="cli", target_id=participant_id, kind="send")

    async def terminate(_provider, generation, _method, params):
        return {
            "operation_id": params["operation_id"],
            "provider_generation": generation,
            "terminal_id": params["terminal_id"],
            "terminal_incarnation": params["terminal_incarnation"],
            "delivery": "accepted",
            "exit_confirmed": True,
        }

    async def backend_unverified(*_args, **_kwargs):
        raise TheaterError("native backend exit is unverified")

    monkeypatch.setattr(daemon.terminal_service.connections, "request", terminate)
    monkeypatch.setattr(participant_rpc, "_require_verified_backend_stop", backend_unverified)
    accepted = await participants_terminate(
        daemon,
        _context(),
        {"participant_id": participant_id},
        idempotency_key="provider-terminate-uncertain",
    )
    await _settle(daemon)

    operation = daemon.operation_service.get(accepted["operation_id"])
    assert operation.state == PublicOperationState.UNCERTAIN.value
    assert daemon.registry.get(participant_id).status is not Status.DEAD
    assert daemon.store.get_job(handle).state == JobState.RUNNING
    assert daemon.store.terminal_bindings.get(participant_id) is not None
    assert daemon.store.workspaces.active_usages("workspace-uncertain")


async def test_verified_termination_cancels_held_public_followup(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = _target(daemon)
    _online(monkeypatch, daemon)
    methods: list[str] = []

    async def request(_provider, generation, method, params):
        methods.append(method)
        result = {
            "operation_id": params["operation_id"],
            "provider_generation": generation,
            "terminal_id": params["terminal_id"],
            "terminal_incarnation": params["terminal_incarnation"],
            "delivery": "accepted",
        }
        if method == "terminal.terminate":
            result["exit_confirmed"] = True
        return result

    monkeypatch.setattr(daemon.terminal_service.connections, "request", request)
    sent = await controls_send(
        daemon,
        _context(),
        {"participant_id": participant_id, "prompt": "active"},
        idempotency_key="terminate-active-send",
    )
    await _settle(daemon)
    queued = await controls_queue_followup(
        daemon,
        _context(),
        {"participant_id": participant_id, "prompt": "must not dispatch"},
        idempotency_key="terminate-held-queue",
    )
    await asyncio.sleep(0)
    queued_before = daemon.operation_service.get(queued["operation_id"])
    assert queued_before.state == PublicOperationState.RUNNING.value

    terminated = await participants_terminate(
        daemon,
        _context(),
        {"participant_id": participant_id},
        idempotency_key="terminate-with-held-queue",
    )
    await _settle(daemon)

    assert daemon.operation_service.get(terminated["operation_id"]).state == "succeeded"
    queued_after = daemon.operation_service.get(queued["operation_id"])
    assert queued_after.state == PublicOperationState.FAILED.value
    control = daemon.store.get_control_operation(queued_after.control_operation_id)
    assert control is not None
    assert control.delivery_phase is ControlDeliveryPhase.SETTLED
    assert control.delivery_result.value == "rejected"
    assert control.error_code == "interrupted"
    assert daemon.store.get_job(queued_after.job_handle).state == JobState.KILLED
    assert (
        daemon.store.get_job(daemon.operation_service.get(sent["operation_id"]).job_handle).state
        == JobState.KILLED
    )
    assert methods == ["terminal.deliver", "terminal.terminate"]


async def test_backend_identity_mismatch_unregisters_live_observation(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = daemon.registry.register(harness="codex", pane=None, cwd=None).id
    binding = ParticipantRuntimeBinding(
        participant_id=participant_id,
        harness="codex",
        wiring=RuntimeWiring.NATIVE,
        backend_generation=4,
        lifecycle=RuntimeLifecyclePhase.ACTIVE,
        endpoint="unix:///tmp/replaced.sock",
        backend_pid=4242,
        backend_started_at=10.0,
        native_session_id="native-replaced",
        created_at=now(),
        updated_at=now(),
    )
    daemon.store.upsert_runtime_binding(binding)
    unregistered: list[str] = []
    monkeypatch.setattr(daemon.runtime_manager, "backend", lambda _participant_id: None)

    async def mismatch(*_args, **_kwargs):
        raise BackendIdentityMismatch("process identity changed")

    monkeypatch.setattr(daemon.runtime_manager, "adopt_backend", mismatch)
    monkeypatch.setattr(daemon.observer.live, "unregister", unregistered.append)

    assert await participant_rpc._stop_verified_detached_backend(daemon, participant_id, binding)
    assert unregistered == [participant_id]
    assert daemon.store.get_runtime_binding(participant_id) == binding


async def test_participant_termination_normalizes_pathological_error(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = _target(daemon)

    class PathologicalTermination(Exception):
        code = "x" * 600

        def __init__(self, message: str) -> None:
            self.details = {"invalid": object(), "nonfinite": float("inf")}
            super().__init__(message)

    async def fail(*_args, **_kwargs):
        raise PathologicalTermination("m" * 9_000)

    monkeypatch.setattr(mutation_handlers, "terminate_participant", fail)
    accepted = await participants_terminate(
        daemon,
        _context(),
        {"participant_id": participant_id},
        idempotency_key="pathological-termination",
    )
    await _settle(daemon)

    operation = daemon.operation_service.get(accepted["operation_id"])
    assert operation.state == PublicOperationState.FAILED.value
    assert operation.phase == "termination_refused"
    assert operation.error is not None
    assert len(str(operation.error["code"])) == 512
    assert len(str(operation.error["message"])) == 8192
    assert "details" not in operation.error
    validator_for("https://theater.dev/schemas/frontend/1.0/common.json#/$defs/operation").validate(
        operation_to_wire(operation)
    )


async def test_public_metadata_and_status_mutations_are_idempotent_and_schema_valid(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = _target(daemon)
    _online(monkeypatch, daemon)
    context = _context()
    updated = await participants_update(
        daemon,
        context,
        {"participant_id": participant_id, "description": "provider worker"},
        idempotency_key="participant-update-a",
    )
    validator_for(METHOD_CATALOG["frontend.participants.update"].result_schema_id).validate(updated)
    duplicate = await participants_update(
        daemon,
        context,
        {"participant_id": participant_id, "description": "provider worker"},
        idempotency_key="participant-update-a",
    )
    assert duplicate == updated
    status = await participants_status(
        daemon,
        context,
        {"participant_id": participant_id, "status": "working"},
        idempotency_key="participant-status-a",
    )
    validator_for(METHOD_CATALOG["frontend.participants.status"].result_schema_id).validate(status)
    assert status["status"] == "working"
