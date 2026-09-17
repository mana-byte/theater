"""RC10 restart recovery and exact provider reclaim."""

from __future__ import annotations

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
from theater.daemon.terminals import ProviderUnavailable, StaleGeneration, TerminalProviderService
from theater.daemon.terminals.service import ProviderReportInvalid
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
    Status,
    WorkspaceOwnershipKind,
    WorkspaceRecord,
    WorkspaceState,
    WorkspaceUsageHolderKind,
    WorkspaceUsageRecord,
)


def _provider() -> ProviderRecord:
    return ProviderRecord(
        provider_id="provider-a",
        selector="fixture",
        kind="test",
        credential_verifier=credential_verifier("credential-a"),
        configuration_version=1,
        capabilities=("terminal-provider.v1",),
        limits={},
        generation=0,
        last_report_revision=None,
        created_at=1.0,
        updated_at=1.0,
    )


def _service(store: Store) -> TerminalProviderService:
    return TerminalProviderService(store, OperationService(store))


def _terminal(generation: int, *, launch_id: str | None = None) -> dict[str, object]:
    identity: dict[str, object] = {
        "provider_id": "provider-a",
        "provider_generation": generation,
        "terminal_id": "terminal-a",
        "terminal_incarnation": "incarnation-a",
        "occupant": {"occupant_id": "participant-a", "harness": "codex"},
        "process": {"pid": 42, "started_at": 2.0, "executable": "/bin/agent"},
    }
    if launch_id is not None:
        identity["launch_id"] = launch_id
    return identity


async def test_provider_generation_cannot_be_acquired_during_startup_recovery(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "provider-startup-gate.db")
    with store.write_unit() as unit:
        store.providers.register(_provider(), connection=unit.connection)
    service = _service(store)

    service.begin_startup_recovery()
    with pytest.raises(ProviderUnavailable, match="daemon_recovery_in_progress"):
        service.authenticate_handshake("provider-a", "credential-a", callback=True)
    assert store.providers.get("provider-a").generation == 0

    service.finish_startup_recovery()
    generation, token = service.authenticate_handshake("provider-a", "credential-a", callback=True)
    assert generation == 1
    assert token is not None
    await service.aclose()
    store.close()


async def test_historical_control_receipt_settles_after_real_database_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-control-recovery.db"
    store = Store(path)
    with store.write_unit() as unit:
        store.providers.register(_provider(), connection=unit.connection)
    first = _service(store)
    generation, _ = first.connections.acquire_callback("provider-a", "credential-a")
    timestamp = 10.0
    with store.write_unit() as unit:
        store.operations.create(
            PublicOperationRecord(
                operation_id="public-send",
                kind="controls.send",
                actor_client_id="operator-a",
                actor_participant_id=None,
                target_ids=("participant-a",),
                state="uncertain",
                phase="provider_ack_lost",
                control_operation_id="control-send",
                job_handle="job-send",
                dispatch_provider_id="provider-a",
                dispatch_provider_generation=generation,
                dispatch_terminal_id="terminal-a",
                dispatch_terminal_incarnation="incarnation-a",
                dispatch_terminal_occupant_evidence={"occupant_id": "participant-a"},
                created_at=timestamp,
                updated_at=timestamp,
                error_code="provider_unavailable",
                error={"code": "provider_unavailable", "message": "ack lost"},
            ),
            connection=unit.connection,
        )
        store.reserve_control_operation(
            ControlOperation(
                operation_id="control-send",
                participant_id="participant-a",
                kind=ControlKind.SEND,
                transport=ControlTransport.PROVIDER_TERMINAL,
                delivery_phase=ControlDeliveryPhase.SETTLED,
                delivery_result=DeliveryResult.UNKNOWN,
                execution_barrier=True,
                job_handle="job-send",
                provider_id="provider-a",
                provider_generation=generation,
                terminal_id="terminal-a",
                terminal_incarnation="incarnation-a",
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
        store.create_job(
            Job(
                handle="job-send",
                caller_id="cli",
                target_id="participant-a",
                kind="send",
                prompt="hello",
                state=JobState.RUNNING.value,
                result=None,
                error_code=None,
                created_at=timestamp,
                finished_at=None,
                actor_client_id="operator-a",
            ),
            connection=unit.connection,
        )
    await first.aclose()
    store.close()

    reopened = Store(path)
    recovered = _service(reopened)
    current_generation, _ = recovered.connections.acquire_callback("provider-a", "credential-a")
    assert current_generation == generation + 1
    report = recovered.report(
        "provider-a",
        current_generation,
        1,
        {
            "receipts": [
                {
                    "operation_id": "public-send",
                    "provider_generation": generation,
                    "terminal_id": "terminal-a",
                    "terminal_incarnation": "incarnation-a",
                    "delivery": "accepted",
                }
            ]
        },
    )
    assert report["reconciled_operation_ids"] == ["public-send"]
    assert reopened.operations.get("public-send").state == "succeeded"
    control = reopened.get_control_operation("control-send")
    assert control.delivery_result is DeliveryResult.ACCEPTED
    assert control.execution_barrier is False
    assert reopened.get_job("job-send").state == JobState.RUNNING
    with pytest.raises(StaleGeneration):
        recovered.report("provider-a", generation, 2, {"receipts": []})
    await recovered.aclose()
    reopened.close()


async def test_lost_create_reclaim_requires_complete_exact_launch_inventory(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-create-recovery.db"
    store = Store(path)
    with store.write_unit() as unit:
        store.providers.register(_provider(), connection=unit.connection)
    first = _service(store)
    dispatched_generation, _ = first.connections.acquire_callback("provider-a", "credential-a")
    timestamp = 10.0
    with store.write_unit() as unit:
        store.upsert_participant(
            Participant(id="participant-a", harness="codex", cwd="/tmp/work"),
            connection=unit.connection,
        )
        store.workspaces.create(
            WorkspaceRecord(
                workspace_id="workspace-a",
                ownership_kind=WorkspaceOwnershipKind.BORROWED.value,
                owner_id="operator-a",
                path="/tmp/work",
                state=WorkspaceState.ACTIVE.value,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
        store.workspaces.acquire_usage(
            WorkspaceUsageRecord(
                usage_id="usage-a",
                workspace_id="workspace-a",
                holder_kind=WorkspaceUsageHolderKind.RESERVATION.value,
                holder_id="operation-create",
                acquired_at=timestamp,
            ),
            connection=unit.connection,
        )
        store.operations.create(
            PublicOperationRecord(
                operation_id="operation-create",
                kind="spawn",
                actor_client_id="operator-a",
                actor_participant_id=None,
                target_ids=("participant-a",),
                state="uncertain",
                phase="terminal_create_outcome_unknown",
                job_handle="participant-a",
                dispatch_provider_id="provider-a",
                dispatch_provider_generation=dispatched_generation,
                created_at=timestamp,
                updated_at=timestamp,
                error_code="provider_unavailable",
                error={"code": "provider_unavailable", "message": "ack lost"},
            ),
            connection=unit.connection,
        )
        store.operations.reserve_launch(
            LaunchReservationRecord(
                operation_id="operation-create",
                participant_id="participant-a",
                provider_id="provider-a",
                workspace_usage_id="usage-a",
                adapter="codex",
                phase="terminal_create_dispatched",
                launch_facts={"provider_generation": dispatched_generation},
                artifact_refs=(),
                dispatch_marker=f"generation:{dispatched_generation}",
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
        store.create_job(
            Job(
                handle="participant-a",
                caller_id=None,
                target_id="participant-a",
                kind="spawn",
                prompt="work",
                state=JobState.RUNNING.value,
                result=None,
                error_code=None,
                created_at=timestamp,
                finished_at=None,
                actor_client_id="operator-a",
            ),
            connection=unit.connection,
        )
        store.operations.claim_idempotency(
            IdempotencyRecord(
                client_id="operator-a",
                key="spawn-a",
                method="frontend.participants.spawn",
                payload_digest="digest",
                operation_id="operation-create",
                response={"operation_id": "operation-create", "state": "accepted"},
                created_at=timestamp,
            ),
            connection=unit.connection,
        )
    await first.aclose()
    store.close()

    reopened = Store(path)
    recovered = _service(reopened)
    current_generation, _ = recovered.connections.acquire_callback("provider-a", "credential-a")
    receipt_terminal = _terminal(dispatched_generation, launch_id="operation-create")
    receipt = {
        "operation_id": "operation-create",
        "launch_id": "operation-create",
        "provider_generation": dispatched_generation,
        "outcome": "accepted",
        "terminal": receipt_terminal,
    }
    incomplete = recovered.report(
        "provider-a",
        current_generation,
        1,
        {
            "terminals": [_terminal(current_generation, launch_id="operation-create")],
            "complete": False,
            "receipts": [receipt],
        },
    )
    assert incomplete["reconciled_operation_ids"] == []
    assert reopened.operations.get("operation-create").state == "uncertain"
    assert reopened.workspaces.get_usage("usage-a").released_at is None

    with pytest.raises(ProviderReportInvalid):
        recovered.report(
            "provider-a",
            current_generation,
            2,
            {
                "terminals": [
                    {
                        **_terminal(current_generation, launch_id="different-launch"),
                    }
                ],
                "complete": True,
                "receipts": [receipt],
            },
        )
    assert reopened.providers.get("provider-a").last_report_revision == 1

    report = recovered.report(
        "provider-a",
        current_generation,
        2,
        {
            "terminals": [_terminal(current_generation, launch_id="operation-create")],
            "complete": True,
            "receipts": [receipt],
        },
    )
    assert report["reconciled_operation_ids"] == ["operation-create"]
    assert reopened.operations.get("operation-create").state == "succeeded"
    binding = reopened.terminal_bindings.get("participant-a")
    assert binding is not None and binding.provider_generation == current_generation
    assert reopened.workspaces.get_usage("usage-a").released_at is not None
    usage = reopened.workspaces.get_active_usage(
        "workspace-a", holder_kind="participant", holder_id="participant-a"
    )
    assert usage is not None
    await recovered.aclose()
    reopened.close()


@pytest.mark.parametrize(
    ("operation_state", "operation_phase"),
    [("accepted", "launch_reserved"), ("running", "launch_preparing")],
)
def test_restart_atomically_fails_never_dispatched_launch_and_releases_reservations(
    tmp_path: Path, operation_state: str, operation_phase: str
) -> None:
    path = tmp_path / "accepted-launch-recovery.db"
    store = Store(path)
    timestamp = 10.0
    with store.write_unit() as unit:
        store.upsert_participant(
            Participant(id="participant-a", harness="codex", cwd="/tmp/work"),
            connection=unit.connection,
        )
        store.workspaces.create(
            WorkspaceRecord(
                workspace_id="workspace-a",
                ownership_kind=WorkspaceOwnershipKind.BORROWED.value,
                owner_id="operator-a",
                path="/tmp/work",
                state=WorkspaceState.ACTIVE.value,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
        store.workspaces.acquire_usage(
            WorkspaceUsageRecord(
                usage_id="usage-a",
                workspace_id="workspace-a",
                holder_kind=WorkspaceUsageHolderKind.RESERVATION.value,
                holder_id="operation-create",
                acquired_at=timestamp,
            ),
            connection=unit.connection,
        )
        store.operations.create(
            PublicOperationRecord(
                operation_id="operation-create",
                kind="spawn",
                actor_client_id="operator-a",
                actor_participant_id=None,
                target_ids=("participant-a",),
                state=operation_state,
                phase=operation_phase,
                job_handle="participant-a",
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
        store.operations.claim_idempotency(
            IdempotencyRecord(
                client_id="operator-a",
                key="accepted-spawn",
                method="frontend.participants.spawn",
                payload_digest="digest",
                operation_id="operation-create",
                response={"operation_id": "operation-create", "state": "accepted"},
                created_at=timestamp,
            ),
            connection=unit.connection,
        )
        store.operations.reserve_launch(
            LaunchReservationRecord(
                operation_id="operation-create",
                participant_id="participant-a",
                provider_id="provider-a",
                workspace_usage_id="usage-a",
                adapter="codex",
                phase="reserved",
                launch_facts={"provider_generation": 1},
                artifact_refs=(),
                dispatch_marker=None,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
        store.create_job(
            Job(
                handle="participant-a",
                caller_id=None,
                target_id="participant-a",
                kind="spawn",
                prompt="work",
                state=JobState.RUNNING.value,
                result=None,
                error_code=None,
                created_at=timestamp,
                finished_at=None,
                actor_client_id="operator-a",
            ),
            connection=unit.connection,
        )
    store.close()

    reopened = Store(path)
    operation_service = OperationService(reopened)
    daemon = SimpleNamespace(
        store=reopened,
        operation_service=operation_service,
        registry=Registry(reopened),
        jobs=JobManager(reopened),
    )
    reconcile_public_control_operations(daemon)

    assert reopened.operations.get("operation-create").state == "failed"
    assert reopened.operations.get_launch("operation-create").phase == "rolled_back"
    assert reopened.get_participant("participant-a").status is Status.DEAD
    assert reopened.get_job("participant-a").state == JobState.CRASHED
    assert reopened.workspaces.get_usage("usage-a").released_at is not None
    idempotency = reopened.operations.get_idempotency("operator-a", "accepted-spawn")
    assert idempotency is not None and idempotency.retain_until is not None
    reopened.close()
