"""Read-only durable evidence classification for explicit reconciliation."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from theater.daemon.operations.service import OperationOutcome, ReconcileEvidence
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
)
from theater.models import PublicOperationRecord, Status, WorkspaceRecord, WorkspaceState


class DurableEvidenceReconciler:
    """Settle only outcomes already proved by durable control/workspace facts."""

    def __init__(
        self,
        store,
        *,
        workspace_project: Callable[[WorkspaceRecord], Mapping[str, object]],
    ) -> None:
        self._store = store
        self._workspace_project = workspace_project

    async def __call__(self, operation: PublicOperationRecord) -> ReconcileEvidence | None:
        control = (
            self._store.get_control_operation(operation.control_operation_id)
            if operation.control_operation_id is not None
            else None
        )
        if control is not None:
            return self._control_evidence(operation, control)
        if operation.kind in {"spawn", "adopt"}:
            return self._terminal_binding_evidence(operation)
        if operation.kind == "participants.terminate":
            return self._termination_evidence(operation)
        if operation.kind == "workspace_cleanup":
            return self._workspace_evidence(operation)
        return None

    def _control_evidence(self, operation, control) -> ReconcileEvidence | None:
        if operation.target_ids != (control.participant_id,):
            return None
        if control.delivery_phase is not ControlDeliveryPhase.SETTLED:
            return None
        error = {
            "code": (control.error_code or "dispatch_failed")[:512],
            "message": (control.error or "the control was rejected")[:8192],
        }
        if control.delivery_result is DeliveryResult.ACCEPTED:
            outcome = OperationOutcome.succeeded(
                phase="delivery_evidence_reconciled",
                result={"delivery": "accepted"},
            )
        elif control.delivery_result is DeliveryResult.REJECTED:
            outcome = OperationOutcome.failed(
                phase="delivery_evidence_reconciled",
                error=error,
            )
        elif self._native_terminal_evidence(control):
            outcome = OperationOutcome.succeeded(
                phase="native_evidence_reconciled",
                result={"delivery": "accepted"},
            )
        else:
            return None
        return ReconcileEvidence(operation.updated_at, outcome)

    def _native_terminal_evidence(self, control) -> bool:
        if (
            control.transport is not ControlTransport.NATIVE_RUNTIME
            or control.kind not in {ControlKind.SEND, ControlKind.QUEUE_FOLLOWUP}
            or control.backend_generation is None
            or control.native_session_id is None
            or control.native_turn_id is None
        ):
            return False
        return (
            self._store.get_native_terminal_evidence(
                participant_id=control.participant_id,
                backend_generation=control.backend_generation,
                native_session_id=control.native_session_id,
                native_turn_id=control.native_turn_id,
            )
            is not None
        )

    def _terminal_binding_evidence(
        self, operation: PublicOperationRecord
    ) -> ReconcileEvidence | None:
        if len(operation.target_ids) != 1:
            return None
        participant_id = operation.target_ids[0]
        binding = self._store.terminal_bindings.get(participant_id)
        expected = (
            operation.dispatch_provider_id,
            operation.dispatch_provider_generation,
            operation.dispatch_terminal_id,
            operation.dispatch_terminal_incarnation,
        )
        current = (
            None
            if binding is None
            else (
                binding.provider_id,
                binding.provider_generation,
                binding.terminal_id,
                binding.terminal_incarnation,
            )
        )
        if None in expected or current != expected:
            return None
        result: dict[str, object] = {"participant_id": participant_id}
        if operation.kind == "spawn":
            launch = self._store.operations.get_launch(operation.operation_id)
            if launch is None or launch.phase != "terminal_bound":
                return None
            result["job_handle"] = operation.job_handle
        return ReconcileEvidence(
            operation.updated_at,
            OperationOutcome.succeeded(phase="terminal_binding_reconciled", result=result),
        )

    def _termination_evidence(self, operation: PublicOperationRecord) -> ReconcileEvidence | None:
        if len(operation.target_ids) != 1 or not any(
            (
                operation.dispatch_provider_id,
                operation.dispatch_backend_generation,
                operation.dispatch_native_session_id,
            )
        ):
            return None
        participant_id = operation.target_ids[0]
        participant = self._store.get_participant(participant_id)
        if (
            participant is None
            or participant.status is not Status.DEAD
            or self._store.terminal_bindings.get(participant_id) is not None
            or self._store.get_runtime_binding(participant_id) is not None
        ):
            return None
        return ReconcileEvidence(
            operation.updated_at,
            OperationOutcome.succeeded(
                phase="participant_exit_reconciled",
                result={"id": participant_id, "killed": True},
            ),
        )

    def _workspace_evidence(self, operation: PublicOperationRecord) -> ReconcileEvidence | None:
        if len(operation.target_ids) != 1:
            return None
        workspace = self._store.workspaces.get(operation.target_ids[0])
        if (
            workspace is None
            or workspace.cleanup_result is None
            or workspace.deletion_operation_id != operation.operation_id
            or workspace.cleanup_force is None
            or workspace.cleanup_delete_branch is None
            or workspace.cleanup_force_branch is None
        ):
            return None
        result = dict(workspace.cleanup_result)
        if result.get("uncertain") is True:
            return None
        value = {**result, "workspace": dict(self._workspace_project(workspace))}
        if result.get("worktree_removed") is not True or result.get("errors"):
            outcome = OperationOutcome.failed(
                phase="workspace_cleanup_evidence_failed",
                error={
                    "code": "bad_request",
                    "message": "workspace cleanup did not complete as requested",
                    "details": value,
                },
            )
        elif workspace.state == WorkspaceState.REMOVED.value:
            outcome = OperationOutcome.succeeded(
                phase="workspace_cleanup_evidence_reconciled",
                result=value,
            )
        else:
            return None
        return ReconcileEvidence(
            operation.updated_at,
            outcome,
        )


__all__ = ["DurableEvidenceReconciler"]
