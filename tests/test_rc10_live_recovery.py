"""Live provider-generation reclaim after daemon database reopen."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

from theater.daemon.jobs import JobManager
from theater.daemon.operations import OperationService
from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.daemon.persistence.store import Store
from theater.daemon.plugins.credentials import credential_verifier
from theater.daemon.registry import Registry
from theater.daemon.runtime.recovery import reconcile_public_control_operations
from theater.daemon.terminals import StaleGeneration, TerminalProviderService
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
)
from theater.models import (
    IdempotencyRecord,
    Job,
    JobState,
    LaunchReservationRecord,
    Participant,
    ProviderRecord,
    PublicOperationRecord,
    TerminalBindingRecord,
    WorkspaceOwnershipKind,
    WorkspaceRecord,
    WorkspaceState,
    WorkspaceUsageHolderKind,
    WorkspaceUsageRecord,
    now,
)


def _terminal(
    generation: int,
    terminal_id: str,
    participant_id: str,
    *,
    launch_id: str | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "provider_id": "provider-a",
        "provider_generation": generation,
        "terminal_id": terminal_id,
        "terminal_incarnation": f"{terminal_id}-incarnation",
        "occupant": {"occupant_id": participant_id, "harness": "codex"},
        "process": {"pid": 42, "started_at": 2.0, "executable": "/bin/agent"},
    }
    if launch_id is not None:
        value["launch_id"] = launch_id
    return value


def _running_job(handle: str, participant_id: str, timestamp: float) -> Job:
    return Job(
        handle=handle,
        caller_id=None,
        target_id=participant_id,
        kind="send",
        prompt="never replay this input",
        state=JobState.RUNNING.value,
        result=None,
        error_code=None,
        created_at=timestamp,
        finished_at=None,
        actor_client_id="recovery-operator",
    )


async def test_reconnect_requires_exact_inventory_and_historical_receipts(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    database = tmp_path / "live-recovery.db"
    timestamp = now()
    store = Store(database)
    with store.write_unit() as unit:
        store.providers.register(
            ProviderRecord(
                provider_id="provider-a",
                selector="fixture",
                kind="test",
                credential_verifier=credential_verifier("credential-a"),
                configuration_version=1,
                capabilities=("terminal-provider.v1",),
                limits={},
                generation=0,
                last_report_revision=None,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
    first_operations = OperationService(store)
    first_service = TerminalProviderService(store, first_operations)
    old_generation, _ = first_service.connections.acquire_callback("provider-a", "credential-a")

    launch_participant = "participant-launch"
    control_participant = "participant-control"
    launch_operation = "operation-launch"
    control_operation = "operation-control"
    launch_terminal = _terminal(
        old_generation,
        "terminal-launch",
        launch_participant,
        launch_id=launch_operation,
    )
    control_terminal = _terminal(old_generation, "terminal-control", control_participant)
    control_occupant = control_terminal["occupant"]
    control_process = control_terminal["process"]
    assert isinstance(control_occupant, Mapping)
    assert isinstance(control_process, Mapping)
    workspace = WorkspaceRecord(
        workspace_id="workspace-a",
        ownership_kind=WorkspaceOwnershipKind.BORROWED.value,
        owner_id="recovery-operator",
        path=str(tmp_path / "workspace"),
        state=WorkspaceState.ACTIVE.value,
        created_at=timestamp,
        updated_at=timestamp,
    )
    with store.write_unit() as unit:
        store.workspaces.create(workspace, connection=unit.connection)
        for participant_id in (launch_participant, control_participant):
            store.upsert_participant(
                Participant(id=participant_id, harness="codex", cwd=workspace.path),
                connection=unit.connection,
            )
        store.workspaces.acquire_usage(
            WorkspaceUsageRecord(
                usage_id="launch-usage",
                workspace_id=workspace.workspace_id,
                holder_kind=WorkspaceUsageHolderKind.RESERVATION.value,
                holder_id=launch_operation,
                acquired_at=timestamp,
            ),
            connection=unit.connection,
        )
        store.create_job(
            _running_job(launch_participant, launch_participant, timestamp),
            connection=unit.connection,
        )
        store.operations.create(
            PublicOperationRecord(
                operation_id=launch_operation,
                kind="spawn",
                actor_client_id="recovery-operator",
                actor_participant_id=None,
                target_ids=(launch_participant,),
                state="uncertain",
                phase="provider_ack_lost",
                job_handle=launch_participant,
                dispatch_provider_id="provider-a",
                dispatch_provider_generation=old_generation,
                created_at=timestamp,
                updated_at=timestamp,
                error_code="provider_unavailable",
                error={"code": "provider_unavailable", "message": "create ack lost"},
            ),
            connection=unit.connection,
        )
        store.operations.reserve_launch(
            LaunchReservationRecord(
                operation_id=launch_operation,
                participant_id=launch_participant,
                provider_id="provider-a",
                workspace_usage_id="launch-usage",
                adapter="codex",
                phase="terminal_create_dispatched",
                launch_facts={"provider_generation": old_generation},
                artifact_refs=(),
                dispatch_marker=f"generation:{old_generation}",
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
        store.operations.claim_idempotency(
            IdempotencyRecord(
                client_id="recovery-operator",
                key="launch-once",
                method="frontend.participants.spawn",
                payload_digest="launch-once",
                operation_id=launch_operation,
                response={"operation_id": launch_operation, "state": "accepted"},
                created_at=timestamp,
            ),
            connection=unit.connection,
        )

        store.terminal_bindings.bind(
            TerminalBindingRecord(
                participant_id=control_participant,
                provider_id="provider-a",
                provider_generation=old_generation,
                terminal_id="terminal-control",
                terminal_incarnation="terminal-control-incarnation",
                occupant_evidence=control_occupant,
                process_facts=control_process,
                health="healthy",
                report_revision=1,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
        control_job = f"{control_participant}#1"
        store.create_job(
            _running_job(control_job, control_participant, timestamp),
            connection=unit.connection,
        )
        store.operations.create(
            PublicOperationRecord(
                operation_id=control_operation,
                kind="controls.send",
                actor_client_id="recovery-operator",
                actor_participant_id=None,
                target_ids=(control_participant,),
                state="uncertain",
                phase="provider_ack_lost",
                control_operation_id=f"{control_operation}:control",
                job_handle=control_job,
                dispatch_provider_id="provider-a",
                dispatch_provider_generation=old_generation,
                dispatch_terminal_id="terminal-control",
                dispatch_terminal_incarnation="terminal-control-incarnation",
                dispatch_terminal_occupant_evidence=control_occupant,
                dispatch_terminal_process_facts=control_process,
                created_at=timestamp,
                updated_at=timestamp,
                error_code="provider_unavailable",
                error={"code": "provider_unavailable", "message": "delivery ack lost"},
            ),
            connection=unit.connection,
        )
        store.reserve_control_operation(
            ControlOperation(
                operation_id=f"{control_operation}:control",
                participant_id=control_participant,
                kind=ControlKind.SEND,
                transport=ControlTransport.PROVIDER_TERMINAL,
                delivery_phase=ControlDeliveryPhase.SETTLED,
                delivery_result=DeliveryResult.UNKNOWN,
                execution_barrier=True,
                job_handle=control_job,
                provider_id="provider-a",
                provider_generation=old_generation,
                terminal_id="terminal-control",
                terminal_incarnation="terminal-control-incarnation",
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
    await first_service.aclose()
    store.close()

    reopened = Store(database)
    operations = OperationService(reopened)
    jobs = JobManager(reopened)
    daemon = SimpleNamespace(
        store=reopened,
        operation_service=operations,
        registry=Registry(reopened),
        jobs=jobs,
    )
    await reconcile_public_control_operations(daemon)
    service = TerminalProviderService(reopened, operations)
    current_generation, _ = service.connections.acquire_callback("provider-a", "credential-a")
    assert current_generation == old_generation + 1

    current_launch = {**launch_terminal, "provider_generation": current_generation}
    current_control = {**control_terminal, "provider_generation": current_generation}
    incomplete = service.report(
        "provider-a",
        current_generation,
        1,
        {"terminals": [current_launch, current_control], "complete": False, "receipts": []},
    )
    assert incomplete["reconciled_operation_ids"] == []
    pending_launch = reopened.operations.get(launch_operation)
    pending_control = reopened.operations.get(control_operation)
    pending_delivery = reopened.get_control_operation(f"{control_operation}:control")
    pending_usage = reopened.workspaces.get_usage("launch-usage")
    assert pending_launch is not None and pending_launch.state == "uncertain"
    assert pending_control is not None and pending_control.state == "uncertain"
    assert pending_delivery is not None and pending_delivery.execution_barrier
    assert pending_usage is not None and pending_usage.released_at is None

    launch_receipt = {
        "operation_id": launch_operation,
        "launch_id": launch_operation,
        "provider_generation": old_generation,
        "outcome": "accepted",
        "terminal": launch_terminal,
    }
    control_receipt = {
        "operation_id": control_operation,
        "provider_generation": old_generation,
        "terminal_id": "terminal-control",
        "terminal_incarnation": "terminal-control-incarnation",
        "delivery": "accepted",
    }
    settled = service.report(
        "provider-a",
        current_generation,
        2,
        {
            "terminals": [current_launch, current_control],
            "complete": True,
            "receipts": [launch_receipt, control_receipt],
        },
    )
    assert set(settled["reconciled_operation_ids"]) == {launch_operation, control_operation}
    recovered_launch = reopened.operations.get(launch_operation)
    recovered_control = reopened.operations.get(control_operation)
    recovered_delivery = reopened.get_control_operation(f"{control_operation}:control")
    assert recovered_launch is not None and recovered_launch.state == "succeeded"
    assert recovered_control is not None and recovered_control.state == "succeeded"
    assert recovered_delivery is not None and recovered_delivery.execution_barrier is False
    binding = reopened.terminal_bindings.get(launch_participant)
    assert binding is not None and binding.provider_generation == current_generation
    released_usage = reopened.workspaces.get_usage("launch-usage")
    assert released_usage is not None and released_usage.released_at is not None
    participant_usage = reopened.workspaces.get_active_usage(
        workspace.workspace_id,
        holder_kind=WorkspaceUsageHolderKind.PARTICIPANT.value,
        holder_id=launch_participant,
    )
    assert participant_usage is not None

    repeated = service.report(
        "provider-a",
        current_generation,
        3,
        {
            "terminals": [current_launch, current_control],
            "complete": True,
            "receipts": [launch_receipt, control_receipt],
        },
    )
    assert repeated["reconciled_operation_ids"] == []
    assert [
        usage
        for usage in reopened.workspaces.active_usages(workspace.workspace_id)
        if usage.holder_kind == WorkspaceUsageHolderKind.PARTICIPANT.value
        and usage.holder_id == launch_participant
    ] == [participant_usage]
    with pytest.raises(StaleGeneration):
        service.report("provider-a", old_generation, 4, {"terminals": [], "complete": True})
    await service.aclose()
    reopened.close()
