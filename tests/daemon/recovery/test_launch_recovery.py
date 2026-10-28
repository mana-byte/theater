"""A restart settles launches whose terminal create was lost or never dispatched."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.provider_recovery import provider_record, provider_service, terminal_identity
from theater.daemon.jobs import JobManager
from theater.daemon.operations import OperationService
from theater.daemon.persistence.store import Store
from theater.daemon.registry import Registry
from theater.daemon.runtime.recovery import reconcile_public_control_operations
from theater.daemon.terminals.service import ProviderReportInvalid
from theater.models import (
    IdempotencyRecord,
    Job,
    JobState,
    LaunchReservationRecord,
    Participant,
    PublicOperationRecord,
    Status,
    WorkspaceOwnershipKind,
    WorkspaceRecord,
    WorkspaceState,
    WorkspaceUsageHolderKind,
    WorkspaceUsageRecord,
)


async def test_lost_create_reclaim_requires_complete_exact_launch_inventory(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-create-recovery.db"
    store = Store(path)
    with store.write_unit() as unit:
        store.providers.register(provider_record(), connection=unit.connection)
    first = provider_service(store)
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
    recovered = provider_service(reopened)
    current_generation, _ = recovered.connections.acquire_callback("provider-a", "credential-a")
    receipt_terminal = terminal_identity(dispatched_generation, launch_id="operation-create")
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
            "terminals": [terminal_identity(current_generation, launch_id="operation-create")],
            "complete": False,
            "receipts": [receipt],
        },
    )
    assert incomplete["reconciled_operation_ids"] == []
    assert incomplete["acknowledged_operation_ids"] == []
    assert incomplete["deferred_operation_ids"] == ["operation-create"]
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
                        **terminal_identity(current_generation, launch_id="different-launch"),
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
            "terminals": [terminal_identity(current_generation, launch_id="operation-create")],
            "complete": True,
            "receipts": [receipt],
        },
    )
    assert report["reconciled_operation_ids"] == ["operation-create"]
    assert report["acknowledged_operation_ids"] == ["operation-create"]
    assert report["deferred_operation_ids"] == []
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
async def test_restart_atomically_fails_never_dispatched_launch_and_releases_reservations(
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
                dispatch_provider_id=("provider-a" if operation_state == "running" else None),
                dispatch_provider_generation=(1 if operation_state == "running" else None),
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
                response_format="json",
                actor_client_id="operator-a",
            ),
            connection=unit.connection,
        )
        store.upsert_participant(
            Participant(id="participant-dispatched", harness="codex", cwd="/tmp/work"),
            connection=unit.connection,
        )
        store.workspaces.acquire_usage(
            WorkspaceUsageRecord(
                usage_id="usage-dispatched",
                workspace_id="workspace-a",
                holder_kind=WorkspaceUsageHolderKind.RESERVATION.value,
                holder_id="operation-dispatched",
                acquired_at=timestamp,
            ),
            connection=unit.connection,
        )
        store.operations.create(
            PublicOperationRecord(
                operation_id="operation-dispatched",
                kind="spawn",
                actor_client_id="operator-a",
                actor_participant_id=None,
                target_ids=("participant-dispatched",),
                state="accepted",
                phase="launch_reserved",
                job_handle="participant-dispatched",
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
        store.operations.reserve_launch(
            LaunchReservationRecord(
                operation_id="operation-dispatched",
                participant_id="participant-dispatched",
                provider_id="provider-a",
                workspace_usage_id="usage-dispatched",
                adapter="codex",
                phase="terminal_create_dispatched",
                launch_facts={"provider_generation": 1},
                artifact_refs=(),
                dispatch_marker="generation:1",
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
        store.create_job(
            Job(
                handle="participant-dispatched",
                caller_id=None,
                target_id="participant-dispatched",
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
    jobs = JobManager(reopened)
    jobs.track("participant-a")
    wake = jobs._events["participant-a"]
    listeners: list[str] = []
    jobs.add_terminal_listener(listeners.append)
    finished_bus_before = [row for row in reopened.bus_tail() if row["kind"] == "job.finished"]
    daemon = SimpleNamespace(
        store=reopened,
        operation_service=operation_service,
        registry=Registry(reopened),
        jobs=jobs,
    )
    await reconcile_public_control_operations(daemon)

    undispatched = reopened.operations.get("operation-create")
    assert undispatched.state == "failed"
    assert undispatched.dispatch_provider_id is None
    assert undispatched.dispatch_provider_generation is None
    assert reopened.operations.get_launch("operation-create").phase == "rolled_back"
    assert reopened.get_participant("participant-a").status is Status.DEAD
    recovered_job = reopened.get_job("participant-a")
    assert recovered_job.state == JobState.CRASHED
    assert (
        recovered_job.response_format == "json"
        and recovered_job.structured_status is None
        and recovered_job.actor_client_id == "operator-a"
    )
    assert wake.is_set() and listeners == []
    assert [row for row in reopened.bus_tail() if row["kind"] == "job.finished"] == (
        finished_bus_before
    )
    assert reopened.workspaces.get_usage("usage-a").released_at is not None
    idempotency = reopened.operations.get_idempotency("operator-a", "accepted-spawn")
    assert idempotency is not None and idempotency.retain_until is not None
    dispatched = reopened.operations.get("operation-dispatched")
    assert dispatched.state == "uncertain"
    assert dispatched.dispatch_provider_id == "provider-a"
    assert dispatched.dispatch_provider_generation == 1
    assert reopened.operations.get_launch("operation-dispatched").phase == (
        "terminal_create_dispatched"
    )
    assert reopened.get_participant("participant-dispatched").status is Status.IDLE
    assert reopened.get_job("participant-dispatched").state == JobState.RUNNING
    assert reopened.workspaces.get_usage("usage-dispatched").released_at is None
    reopened.close()


async def test_accepted_creation_receipt_reconciliation_keeps_a_crashed_spawn_job(
    tmp_path: Path,
) -> None:
    """Reconciling the terminal an accepted spawn created never rewrites its crashed job.

    The spawn's initial prompt was refused before any operation was reserved, so the
    job crashed with the refusal while the public operation stayed uncertain; the
    provider's accepted creation receipt settles only the operation and the binding.
    """
    store = Store(tmp_path / "crashed-spawn.db")
    timestamp = 10.0
    with store.write_unit() as unit:
        store.providers.register(provider_record(), connection=unit.connection)
        store.upsert_participant(
            Participant(id="participant-a", harness="codex", cwd="/tmp/work"),
            connection=unit.connection,
        )
    service = provider_service(store)
    generation, _ = service.connections.acquire_callback("provider-a", "credential-a")
    with store.write_unit() as unit:
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
                dispatch_provider_generation=generation,
                created_at=timestamp,
                updated_at=timestamp,
                error_code="bad_request",
                error={"code": "bad_request", "message": "does not support send"},
            ),
            connection=unit.connection,
        )
        store.operations.reserve_launch(
            LaunchReservationRecord(
                operation_id="operation-create",
                participant_id="participant-a",
                provider_id="provider-a",
                workspace_usage_id=None,
                adapter="codex",
                phase="terminal_create_dispatched",
                launch_facts={"provider_generation": generation},
                artifact_refs=(),
                dispatch_marker=f"generation:{generation}",
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
        store.create_job(
            Job(
                handle="participant-a",
                caller_id="cli",
                target_id="participant-a",
                kind="spawn",
                prompt="work",
                state=JobState.CRASHED.value,
                result="participant 'participant-a' does not support send",
                error_code="bad_request",
                created_at=timestamp,
                finished_at=timestamp,
                actor_client_id="operator-a",
            ),
            connection=unit.connection,
        )
    report = service.report(
        "provider-a",
        generation,
        1,
        {
            "terminals": [terminal_identity(generation, launch_id="operation-create")],
            "complete": True,
            "receipts": [
                {
                    "operation_id": "operation-create",
                    "launch_id": "operation-create",
                    "provider_generation": generation,
                    "outcome": "accepted",
                    "terminal": terminal_identity(generation, launch_id="operation-create"),
                }
            ],
        },
    )
    assert report["reconciled_operation_ids"] == ["operation-create"]
    assert store.operations.get("operation-create").state == "succeeded"
    assert store.terminal_bindings.get("participant-a") is not None
    job = store.get_job("participant-a")
    assert job.state == JobState.CRASHED
    assert job.error_code == "bad_request"
    assert "does not support send" in (job.result or "")
    await service.aclose()
    store.close()
