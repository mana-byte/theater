"""Cleanup of unique workspaces after an explicitly requested, verified exit."""

from __future__ import annotations

import logging

from theater.models import Participant, TheaterError, WorkspaceOwnershipKind

logger = logging.getLogger("theater.worktrees")


async def cleanup_killed_workspace(daemon, participant: Participant) -> dict[str, object] | None:
    """Use the durable cleanup operation so partial removal remains recoverable."""
    if participant.workspace_id is None:
        return None
    workspace_id = participant.workspace_id
    try:
        workspace = daemon.workspace_service.get(workspace_id)
        if workspace.ownership_kind != WorkspaceOwnershipKind.THEATER.value or workspace.name:
            return None
        accepted = daemon.workspace_service.cleanup(
            client_id="participant-kill",
            actor_participant_id=participant.id,
            idempotency_key=f"kill-{participant.id}-{workspace_id}",
            params={
                "workspace_id": workspace_id,
                "force": False,
                "delete_branch": True,
                "force_branch": False,
            },
        )
        operation_id = str(accepted["operation_id"])
        operation, pending = await daemon.operation_service.wait(operation_id, wait_seconds=10)
    except TheaterError as exc:
        return {
            "workspace_id": workspace_id,
            "state": "retained",
            "error": {"code": exc.code, "message": str(exc)},
        }
    except Exception:
        logger.exception("post-kill workspace cleanup failed for %s", participant.id)
        return {
            "workspace_id": workspace_id,
            "state": "unknown",
            "error": {
                "code": "internal",
                "message": (
                    f"inspect `theater workspaces get {workspace_id}` before retrying cleanup"
                ),
            },
        }
    return {
        "workspace_id": workspace_id,
        "operation_id": operation_id,
        "state": operation.state,
        "pending": pending,
        "result": operation.result,
        "error": operation.error,
    }
