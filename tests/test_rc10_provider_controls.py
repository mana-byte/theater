"""Provider-routed controls retain durable linkage and exact terminal fences."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import update

import theater.daemon.frontend.participant_mutation_handlers as mutation_handlers
from tests._presence_doubles import UnknownPresence
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
from theater.daemon.frontend.participant_mutation_handlers import (
    participants_status,
    participants_terminate,
    participants_update,
)
from theater.daemon.harness_runtime.errors import BackendIdentityMismatch
from theater.daemon.operations import operation_to_wire
from theater.daemon.persistence.repositories.runtime_bindings import ParticipantRuntimeBinding
from theater.daemon.rpc import participants as participant_rpc
from theater.daemon.schema import terminal_bindings
from theater.daemon.terminals import CallbackOutcomeUnknown
from theater.frontend.capabilities import METHOD_CATALOG, ConnectionChannel, ConnectionRole
from theater.frontend.schemas import validate_callback_request, validator_for
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlTransport,
    RuntimeCapability,
    RuntimeLifecyclePhase,
    RuntimeWiring,
)
from theater.models import (
    JobState,
    PublicOperationState,
    Status,
    TerminalBindingRecord,
    TheaterError,
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


def _online(monkeypatch: pytest.MonkeyPatch, daemon) -> None:
    monkeypatch.setattr(daemon.terminal_service.connections, "is_current", lambda *_: True)
    monkeypatch.setattr(daemon.terminal_service.connections, "health", lambda *_: "online")


async def _settle(daemon) -> None:
    await asyncio.sleep(0)
    tasks = daemon.operation_service.owned_tasks
    if tasks:
        await asyncio.gather(*tasks)


def _accepted(method: str, value: object) -> None:
    validator_for(METHOD_CATALOG[method].result_schema_id).validate(value)


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


async def test_provider_unknown_keeps_barrier_and_queued_followup(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = _target(daemon)
    _online(monkeypatch, daemon)

    async def unknown(_provider, _generation, _method, _params):
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

    queued = await controls_queue_followup(
        daemon,
        _context(),
        {"participant_id": participant_id, "prompt": "later"},
        idempotency_key="provider-queue-a",
    )
    await asyncio.sleep(0)
    queued_operation = daemon.operation_service.get(queued["operation_id"])
    assert queued_operation.state == PublicOperationState.RUNNING.value
    control = daemon.store.get_control_operation(queued_operation.control_operation_id)
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
    await asyncio.sleep(0)
    queued_operation = daemon.operation_service.get(queued["operation_id"])
    control = daemon.store.get_control_operation(queued_operation.control_operation_id)
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
    accepted = await controls_send(
        daemon,
        _context(),
        {"participant_id": participant_id, "prompt": "blocked"},
        idempotency_key="provider-presence-blocked",
    )
    await _settle(daemon)
    operation = daemon.operation_service.get(accepted["operation_id"])
    assert operation.state == PublicOperationState.FAILED.value
    assert operation.control_operation_id is None
    assert not dispatched


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
    daemon,
) -> None:
    participant_id = _target(daemon)
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
