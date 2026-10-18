"""Production reconcilers settle controls only on exact durable evidence."""

from __future__ import annotations

import pytest
from sqlalchemy import update

from tests.support.builders import operator_context
from theater.daemon.frontend.operation_handlers import operations_reconcile
from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.daemon.persistence.repositories.native_evidence import NativeTerminalEvidence
from theater.daemon.schema import workspaces
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
    NativeTurnTerminal,
    ResultCompleteness,
    ResultProvenance,
)
from theater.models import (
    PublicOperationRecord,
    PublicOperationState,
    WorkspaceOwnershipKind,
    WorkspaceRecord,
    WorkspaceState,
    now,
)


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
        operator_context(),
        {"operation_id": operation_id},
    )

    assert reconciled["state"] == PublicOperationState.SUCCEEDED.value
    assert reconciled["phase"] == "delivery_evidence_reconciled"
    assert reconciled["result"] == {"delivery": "accepted"}


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
        operator_context(),
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
        operator_context(),
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
        operator_context(),
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
        operator_context(),
        {"operation_id": operation_id},
    )

    assert reconciled["state"] == PublicOperationState.UNCERTAIN.value
    assert reconciled["phase"] == "outcome_persistence_error"
