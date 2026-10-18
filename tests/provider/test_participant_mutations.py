"""Provider-routed kill, termination, metadata and status mutations."""

from __future__ import annotations

import asyncio

import pytest

import theater.daemon.frontend.participant_mutation_handlers as mutation_handlers
from tests.support.builders import operator_context
from tests.support.provider_controls import online, provider_target
from tests.support.provider_native import assert_result_valid
from tests.support.waiting import settle_operations
from theater.daemon.frontend.control_handlers import (
    controls_queue_followup,
    controls_send,
)
from theater.daemon.frontend.participant_mutation_handlers import (
    participants_status,
    participants_terminate,
    participants_update,
)
from theater.daemon.harness_runtime.errors import BackendIdentityMismatch
from theater.daemon.operations import operation_to_wire
from theater.daemon.persistence.repositories.runtime_bindings import ParticipantRuntimeBinding
from theater.daemon.rpc import participants as participant_rpc
from theater.frontend.capabilities import METHOD_CATALOG
from theater.frontend.schemas import validator_for
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    RuntimeLifecyclePhase,
    RuntimeWiring,
)
from theater.models import (
    JobState,
    PublicOperationState,
    Status,
    TheaterError,
    WorkspaceOwnershipKind,
    WorkspaceRecord,
    WorkspaceState,
    WorkspaceUsageHolderKind,
    WorkspaceUsageRecord,
    now,
)


async def test_kill_admits_on_cached_absence_and_defers_to_provider_check(daemon, monkeypatch):
    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)
    await daemon.presence.require_absent(participant_id)  # cache fresh absence
    inspect_calls, params_seen = [], []
    monkeypatch.setattr(
        daemon.terminal_service, "inspect", lambda *args, **kwargs: inspect_calls.append(args)
    )

    async def terminate(_provider_id, generation, _method, params):
        params_seen.append(params)
        return {
            **{key: params[key] for key in ("operation_id", "terminal_id", "terminal_incarnation")},
            "provider_generation": generation,
            "delivery": "accepted",
            "exit_confirmed": True,
        }

    monkeypatch.setattr(daemon.terminal_service.connections, "request", terminate)
    assert (await participant_rpc._kill(daemon, {"id": participant_id}))["killed"] is True
    assert inspect_calls == []
    assert params_seen[0]["require_absent"] is True


@pytest.mark.parametrize("delivery", ["accepted", "unknown", "cancelled"])
async def test_private_kill_persists_receipt_identity_before_dispatch(
    daemon, monkeypatch, delivery, caplog
):
    caplog.set_level("INFO", logger="theater.timing")
    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)
    receipts = []
    inspect_calls = []
    original_inspect = daemon.terminal_service.inspect

    async def inspect(*args, **kwargs):
        inspect_calls.append(args)
        return await original_inspect(*args, **kwargs)

    monkeypatch.setattr(daemon.terminal_service, "inspect", inspect)

    async def terminate(provider_id, generation, method, params):
        assert method == "terminal.terminate"
        operation = daemon.operation_service.get(params["operation_id"])
        assert operation.kind == "participants.terminate"
        assert operation.state == "running"
        assert operation.dispatch_provider_id == provider_id
        assert operation.dispatch_provider_generation == generation
        assert operation.dispatch_terminal_id == params["terminal_id"]
        receipt = {
            "operation_id": params["operation_id"],
            "provider_generation": generation,
            "terminal_id": params["terminal_id"],
            "terminal_incarnation": params["terminal_incarnation"],
            "delivery": "unknown" if delivery == "cancelled" else delivery,
            "exit_confirmed": delivery == "accepted",
        }
        receipts.append(receipt)
        if delivery == "cancelled":
            raise asyncio.CancelledError
        return receipt

    monkeypatch.setattr(daemon.terminal_service.connections, "request", terminate)
    if delivery == "accepted":
        result = await participant_rpc._kill(daemon, {"id": participant_id})
        assert result["killed"] is True
    else:
        with pytest.raises(asyncio.CancelledError if delivery == "cancelled" else TheaterError):
            await participant_rpc._kill(daemon, {"id": participant_id})
        assert daemon.registry.get(participant_id).status is not Status.DEAD
    operation = daemon.operation_service.get(receipts[0]["operation_id"])
    assert operation.state == ("succeeded" if delivery == "accepted" else "uncertain")
    assert daemon.terminal_service.recovery.validate("provider-a", receipts) == ()
    assert len(inspect_calls) == 1
    for stage in ("lock_wait", "presence", "provider"):
        assert any(
            record.message.startswith(f"kill.{stage} ")
            and f"operation={operation.operation_id}" in record.message
            for record in caplog.records
        )


async def test_verified_provider_termination_releases_usage_but_retains_workspace(
    daemon, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)
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
        operator_context(),
        {"participant_id": participant_id},
        idempotency_key="provider-terminate-a",
    )
    assert_result_valid("frontend.participants.terminate", accepted)
    await settle_operations(daemon)
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
    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)
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
        operator_context(),
        {"participant_id": participant_id},
        idempotency_key="provider-terminate-uncertain",
    )
    await settle_operations(daemon)

    operation = daemon.operation_service.get(accepted["operation_id"])
    assert operation.state == PublicOperationState.UNCERTAIN.value
    assert daemon.registry.get(participant_id).status is not Status.DEAD
    assert daemon.store.get_job(handle).state == JobState.RUNNING
    assert daemon.store.terminal_bindings.get(participant_id) is not None
    assert daemon.store.workspaces.active_usages("workspace-uncertain")


async def test_verified_termination_cancels_held_public_followup(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)
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
        operator_context(),
        {"participant_id": participant_id, "prompt": "active"},
        idempotency_key="terminate-active-send",
    )
    await settle_operations(daemon)
    queued = await controls_queue_followup(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "prompt": "must not dispatch"},
        idempotency_key="terminate-held-queue",
    )
    await asyncio.sleep(0)
    queued_before = daemon.operation_service.get(queued["operation_id"])
    assert queued_before.state == PublicOperationState.RUNNING.value

    terminated = await participants_terminate(
        daemon,
        operator_context(),
        {"participant_id": participant_id},
        idempotency_key="terminate-with-held-queue",
    )
    await settle_operations(daemon)

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
    participant_id = provider_target(daemon)

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
        operator_context(),
        {"participant_id": participant_id},
        idempotency_key="pathological-termination",
    )
    await settle_operations(daemon)

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
    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)
    context = operator_context()
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
