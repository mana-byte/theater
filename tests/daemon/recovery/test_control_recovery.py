"""A restart settles control operations and dispatch receipts without replaying input."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.provider_recovery import provider_record, provider_service, terminal_identity
from theater.config import RetentionSection
from theater.daemon.gc import sweep
from theater.daemon.jobs import JobManager
from theater.daemon.operations import OperationService
from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.daemon.persistence.store import Store
from theater.daemon.registry import Registry
from theater.daemon.runtime.recovery import reconcile_public_control_operations
from theater.daemon.terminals import (
    ProviderReceiptReconciler,
    StaleGeneration,
)
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
    JournalEventRecord,
    LaunchReservationRecord,
    Participant,
    PublicOperationRecord,
    Status,
    TerminalBindingRecord,
    WorkspaceOwnershipKind,
    WorkspaceRecord,
    WorkspaceState,
    WorkspaceUsageHolderKind,
    WorkspaceUsageRecord,
    now,
)


async def test_historical_control_receipt_settles_after_real_database_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-control-recovery.db"
    store = Store(path)
    with store.write_unit() as unit:
        store.providers.register(provider_record(), connection=unit.connection)
    first = provider_service(store)
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
    recovered = provider_service(reopened)
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


async def test_private_control_receipt_advances_report_and_settles_exact_control(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private-provider-control-recovery.db"
    store = Store(path)
    with store.write_unit() as unit:
        store.providers.register(provider_record(), connection=unit.connection)
    first = provider_service(store)
    generation, _ = first.connections.acquire_callback("provider-a", "credential-a")
    timestamp = 10.0
    with store.write_unit() as unit:
        store.upsert_participant(
            Participant(id="participant-a", harness="codex", cwd="/tmp/work"),
            connection=unit.connection,
        )
        store.terminal_bindings.bind(
            TerminalBindingRecord(
                participant_id="participant-a",
                provider_id="provider-a",
                provider_generation=generation,
                terminal_id="terminal-a",
                terminal_incarnation="incarnation-a",
                occupant_evidence={"occupant_id": "participant-a", "harness": "codex"},
                process_facts={"pid": 42, "started_at": 2.0, "executable": "/bin/agent"},
                health="healthy",
                report_revision=1,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
        store.reserve_control_operation(
            ControlOperation(
                operation_id="private-control",
                participant_id="participant-a",
                kind=ControlKind.SEND,
                transport=ControlTransport.PROVIDER_TERMINAL,
                delivery_phase=ControlDeliveryPhase.SETTLED,
                delivery_result=DeliveryResult.UNKNOWN,
                execution_barrier=True,
                provider_id="provider-a",
                provider_generation=generation,
                terminal_id="terminal-a",
                terminal_incarnation="incarnation-a",
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
    await first.aclose()
    store.close()

    reopened = Store(path)
    recovered = provider_service(reopened)
    current_generation, _ = recovered.connections.acquire_callback("provider-a", "credential-a")
    receipt = {
        "method": "terminal.deliver",
        "operation_id": "private-control",
        "provider_generation": generation,
        "terminal_id": "terminal-a",
        "terminal_incarnation": "incarnation-a",
        "delivery": "accepted",
        "terminal": terminal_identity(generation),
    }
    replaced = {**receipt, "terminal": terminal_identity(generation)}
    replaced["terminal"]["occupant"] = {"occupant_id": "participant-other"}
    with pytest.raises(ProviderReportInvalid, match="occupant evidence"):
        recovered.report("provider-a", current_generation, 1, {"receipts": [replaced]})
    assert reopened.providers.get("provider-a").last_report_revision is None

    report = recovered.report("provider-a", current_generation, 1, {"receipts": [receipt]})
    assert report["reconciled_operation_ids"] == ["private-control"]
    assert reopened.providers.get("provider-a").last_report_revision == 1
    control = reopened.get_control_operation("private-control")
    assert control.delivery_result is DeliveryResult.ACCEPTED
    assert control.execution_barrier is False
    await recovered.aclose()
    reopened.close()


@pytest.mark.parametrize(
    ("delivery_phase", "delivery_result"),
    [
        (ControlDeliveryPhase.DISPATCHED, None),
        (ControlDeliveryPhase.SETTLED, DeliveryResult.UNKNOWN),
    ],
)
async def test_accepted_public_control_with_possible_dispatch_recovers_as_uncertain(
    daemon,
    delivery_phase: ControlDeliveryPhase,
    delivery_result: DeliveryResult | None,
) -> None:
    participant = daemon.registry.create_spawned(harness="codex", cwd="/tmp", has_prompt=False)
    operation_id = f"public-{delivery_phase.value}"
    control_id = f"{operation_id}:control"
    job_handle = f"{participant.id}#recovery"
    daemon.jobs.create(
        handle=job_handle,
        caller_id="cli",
        target_id=participant.id,
        kind="send",
        prompt="do not replay",
        actor_client_id="operator-a",
    )
    timestamp = 10.0
    with daemon.store.write_unit() as unit:
        daemon.store.providers.register(provider_record(), connection=unit.connection)
        daemon.store.terminal_bindings.bind(
            TerminalBindingRecord(
                participant_id=participant.id,
                provider_id="provider-a",
                provider_generation=3,
                terminal_id="terminal-a",
                terminal_incarnation="incarnation-a",
                occupant_evidence={"occupant_id": participant.id, "harness": "codex"},
                process_facts={"pid": 42, "started_at": 2.0},
                health="healthy",
                report_revision=1,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
        daemon.store.operations.create(
            PublicOperationRecord(
                operation_id=operation_id,
                kind="controls.send",
                actor_client_id="operator-a",
                actor_participant_id=None,
                target_ids=(participant.id,),
                state="accepted",
                phase="accepted",
                job_handle=job_handle,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
        daemon.store.reserve_control_operation(
            ControlOperation(
                operation_id=control_id,
                participant_id=participant.id,
                kind=ControlKind.SEND,
                transport=ControlTransport.PROVIDER_TERMINAL,
                delivery_phase=delivery_phase,
                delivery_result=delivery_result,
                execution_barrier=True,
                job_handle=job_handle,
                provider_id="provider-a",
                provider_generation=3,
                terminal_id="terminal-a",
                terminal_incarnation="incarnation-a",
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )

    await reconcile_public_control_operations(daemon)

    operation = daemon.operation_service.get(operation_id)
    assert operation.state == "uncertain"
    assert operation.control_operation_id == control_id
    assert operation.dispatch_provider_id == "provider-a"
    assert operation.dispatch_provider_generation == 3
    assert operation.dispatch_terminal_id == "terminal-a"
    assert operation.dispatch_terminal_incarnation == "incarnation-a"
    assert operation.dispatch_terminal_occupant_evidence == {
        "occupant_id": participant.id,
        "harness": "codex",
    }
    control = daemon.store.get_control_operation(control_id)
    assert control.delivery_phase is delivery_phase
    assert control.delivery_result is delivery_result
    assert control.execution_barrier is True
    assert daemon.store.get_job(job_handle).state == JobState.RUNNING

    receipt = {
        "operation_id": operation_id,
        "provider_generation": 3,
        "terminal_id": "terminal-a",
        "terminal_incarnation": "incarnation-a",
        "delivery": "accepted",
    }
    reconciler = ProviderReceiptReconciler(daemon.store, daemon.operation_service)
    reconciler.configure_runtime(controls=daemon.controls, jobs=daemon.jobs)
    reconciler.validate("provider-a", [receipt])
    with daemon.store.write_unit() as unit:
        first = daemon.store.journal.current_sequence(connection=unit.connection) + 1
        reconciled, deferred, events = reconciler.reconcile(
            unit,
            provider_id="provider-a",
            current_generation=4,
            report_revision=1,
            inventory_complete=False,
            terminals=(),
            receipts=[receipt],
            timestamp=20.0,
            first_revision=first,
        )
        daemon.store.journal.append_group(unit, events)
    assert reconciled == (operation_id,)
    assert deferred == ()
    assert daemon.operation_service.get(operation_id).state == "succeeded"
    assert daemon.store.get_control_operation(control_id).delivery_result is (
        DeliveryResult.ACCEPTED
    )
    assert daemon.store.get_control_operation(control_id).execution_barrier is False


async def test_running_identity_free_termination_recovers_as_uncertain(daemon) -> None:
    participant = daemon.registry.create_spawned(harness="codex", cwd="/tmp", has_prompt=False)
    timestamp = 10.0
    with daemon.store.write_unit() as unit:
        daemon.store.operations.create(
            PublicOperationRecord(
                operation_id="termination-without-route-identity",
                kind="participants.terminate",
                actor_client_id="operator-a",
                actor_participant_id=None,
                target_ids=(participant.id,),
                state="running",
                phase="termination_preparing",
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )

    await reconcile_public_control_operations(daemon)
    await reconcile_public_control_operations(daemon)

    operation = daemon.operation_service.get("termination-without-route-identity")
    assert operation.state == "uncertain"
    assert operation.phase == "mutation_recovery_pending"
    assert operation.error_code == "daemon_restarted"
    assert operation.control_operation_id is None
    assert daemon.registry.get(participant.id).status is not Status.DEAD


def _job(handle: str, target_id: str, timestamp: float) -> Job:
    return Job(
        handle=handle,
        caller_id=None,
        target_id=target_id,
        kind="send",
        prompt="must not replay",
        state=JobState.RUNNING.value,
        result=None,
        error_code=None,
        created_at=timestamp,
        finished_at=None,
        actor_client_id="fault-operator",
    )


def _operation(
    operation_id: str,
    kind: str,
    target_id: str,
    job_handle: str,
    timestamp: float,
    *,
    control_operation_id: str | None = None,
) -> PublicOperationRecord:
    return PublicOperationRecord(
        operation_id=operation_id,
        kind=kind,
        actor_client_id="fault-operator",
        actor_participant_id=None,
        target_ids=(target_id,),
        state="accepted",
        phase="accepted",
        control_operation_id=control_operation_id,
        job_handle=job_handle,
        created_at=timestamp,
        updated_at=timestamp,
    )


def test_crash_before_write_unit_commit_publishes_neither_state_nor_event(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pre-commit-crash.db"
    store = Store(database)
    initial_sequence = store.journal.current_sequence()
    with (
        pytest.raises(RuntimeError, match="simulated process crash"),
        store.write_unit() as unit,
    ):
        operation = _operation(
            "uncommitted-operation",
            "controls.send",
            "participant-a",
            "job-a",
            now(),
        )
        store.operations.create(operation, connection=unit.connection)
        store.journal.append_group(
            unit,
            [
                JournalEventRecord(
                    kind="operation.updated",
                    entity_id=operation.operation_id,
                    entity_revision=initial_sequence + 1,
                    payload={"operation_id": operation.operation_id},
                    recorded_at=operation.created_at,
                )
            ],
        )
        raise RuntimeError("simulated process crash")
    store.close()

    reopened = Store(database)
    assert reopened.operations.get("uncommitted-operation") is None
    assert reopened.journal.current_sequence() == initial_sequence
    reopened.close()


async def test_commit_dispatch_receipt_settlement_matrix_survives_reopen_and_gc(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    """Crash facts classify work without replay or releasing uncertain resources."""
    database = tmp_path / "fault-matrix.db"
    timestamp = now() - 10 * 86_400
    store = Store(database)
    workspace = WorkspaceRecord(
        workspace_id="workspace-a",
        ownership_kind=WorkspaceOwnershipKind.BORROWED.value,
        owner_id="fault-operator",
        path=str(tmp_path / "checkout"),
        state=WorkspaceState.ACTIVE.value,
        created_at=timestamp,
        updated_at=timestamp,
    )
    with store.write_unit() as unit:
        store.workspaces.create(workspace, connection=unit.connection)
        for suffix in ("undispatched", "dispatched"):
            participant_id = f"participant-{suffix}"
            operation_id = f"spawn-{suffix}"
            usage_id = f"usage-{suffix}"
            store.upsert_participant(
                Participant(id=participant_id, harness="codex", cwd=workspace.path),
                connection=unit.connection,
            )
            store.create_job(
                _job(participant_id, participant_id, timestamp), connection=unit.connection
            )
            store.workspaces.acquire_usage(
                WorkspaceUsageRecord(
                    usage_id=usage_id,
                    workspace_id=workspace.workspace_id,
                    holder_kind=WorkspaceUsageHolderKind.RESERVATION.value,
                    holder_id=operation_id,
                    acquired_at=timestamp,
                ),
                connection=unit.connection,
            )
            store.operations.create(
                _operation(operation_id, "spawn", participant_id, participant_id, timestamp),
                connection=unit.connection,
            )
            store.operations.reserve_launch(
                LaunchReservationRecord(
                    operation_id=operation_id,
                    participant_id=participant_id,
                    provider_id="provider-a",
                    workspace_usage_id=usage_id,
                    adapter="codex",
                    phase=("terminal_create_dispatched" if suffix == "dispatched" else "reserved"),
                    launch_facts={"provider_generation": 3},
                    artifact_refs=(),
                    dispatch_marker=("generation:3" if suffix == "dispatched" else None),
                    created_at=timestamp,
                    updated_at=timestamp,
                ),
                connection=unit.connection,
            )
            store.operations.claim_idempotency(
                IdempotencyRecord(
                    client_id="fault-operator",
                    key=operation_id,
                    method="frontend.participants.spawn",
                    payload_digest=operation_id,
                    operation_id=operation_id,
                    response={"operation_id": operation_id, "state": "accepted"},
                    created_at=timestamp,
                ),
                connection=unit.connection,
            )

        participant_id = "participant-dispatched"
        store.terminal_bindings.bind(
            TerminalBindingRecord(
                participant_id=participant_id,
                provider_id="provider-a",
                provider_generation=3,
                terminal_id="terminal-a",
                terminal_incarnation="incarnation-a",
                occupant_evidence={"occupant_id": participant_id, "harness": "codex"},
                process_facts={"pid": 42, "started_at": 2.0},
                health="healthy",
                report_revision=1,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
        for suffix, phase, result, barrier in (
            ("possible", ControlDeliveryPhase.DISPATCHED, None, True),
            ("receipt", ControlDeliveryPhase.SETTLED, DeliveryResult.ACCEPTED, False),
        ):
            operation_id = f"control-{suffix}"
            control_id = f"{operation_id}:control"
            job_handle = f"{participant_id}#{suffix}"
            store.create_job(
                _job(job_handle, participant_id, timestamp), connection=unit.connection
            )
            store.operations.create(
                _operation(
                    operation_id,
                    "controls.send",
                    participant_id,
                    job_handle,
                    timestamp,
                    control_operation_id=control_id,
                ),
                connection=unit.connection,
            )
            store.reserve_control_operation(
                ControlOperation(
                    operation_id=control_id,
                    participant_id=participant_id,
                    kind=ControlKind.SEND,
                    transport=ControlTransport.PROVIDER_TERMINAL,
                    delivery_phase=phase,
                    delivery_result=result,
                    execution_barrier=barrier,
                    job_handle=job_handle,
                    provider_id="provider-a",
                    provider_generation=3,
                    terminal_id="terminal-a",
                    terminal_incarnation="incarnation-a",
                    created_at=timestamp,
                    updated_at=timestamp,
                ),
                connection=unit.connection,
            )
    store.close()

    reopened = Store(database)
    jobs = JobManager(reopened)
    daemon = SimpleNamespace(
        store=reopened,
        operation_service=OperationService(reopened),
        registry=Registry(reopened),
        jobs=jobs,
    )
    await reconcile_public_control_operations(daemon)

    undispatched = reopened.operations.get("spawn-undispatched")
    undispatched_participant = reopened.get_participant("participant-undispatched")
    undispatched_job = reopened.get_job("participant-undispatched")
    undispatched_usage = reopened.workspaces.get_usage("usage-undispatched")
    assert undispatched is not None and undispatched.state == "failed"
    assert undispatched_participant is not None
    assert undispatched_participant.status is Status.DEAD
    assert undispatched_job is not None and undispatched_job.state == JobState.CRASHED
    assert undispatched_usage is not None and undispatched_usage.released_at is not None

    dispatched = reopened.operations.get("spawn-dispatched")
    dispatched_job = reopened.get_job("participant-dispatched")
    dispatched_usage = reopened.workspaces.get_usage("usage-dispatched")
    assert dispatched is not None
    assert dispatched.state == "uncertain"
    assert dispatched.dispatch_provider_id == "provider-a"
    assert dispatched.dispatch_provider_generation == 3
    assert dispatched_job is not None and dispatched_job.state == JobState.RUNNING
    assert dispatched_usage is not None and dispatched_usage.released_at is None

    possible = reopened.operations.get("control-possible")
    possible_control = reopened.get_control_operation("control-possible:control")
    settled_operation = reopened.operations.get("control-receipt")
    settled_control = reopened.get_control_operation("control-receipt:control")
    assert possible is not None and possible.state == "uncertain"
    assert possible_control is not None and possible_control.execution_barrier is True
    assert settled_operation is not None and settled_operation.state == "succeeded"
    assert settled_control is not None
    assert settled_control.delivery_result is DeliveryResult.ACCEPTED

    await sweep(
        reopened,
        RetentionSection(
            bus_days=1,
            events_days=1,
            jobs_days=1,
            stale_running_days=1,
            batch=10,
        ),
        live_handles=frozenset({"participant-dispatched", "participant-dispatched#possible"}),
    )
    retained_operation = reopened.operations.get("spawn-dispatched")
    retained_workspace = reopened.workspaces.get("workspace-a")
    retained_usage = reopened.workspaces.get_usage("usage-dispatched")
    assert retained_operation is not None and retained_operation.state == "uncertain"
    assert retained_workspace is not None
    assert retained_workspace.state == WorkspaceState.ACTIVE.value
    assert retained_usage is not None and retained_usage.released_at is None
    claim = reopened.operations.get_idempotency("fault-operator", "spawn-dispatched")
    assert claim is not None and claim.operation_id == "spawn-dispatched"
    reopened.close()
