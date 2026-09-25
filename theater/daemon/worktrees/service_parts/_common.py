"""Durable workspace lifecycle, usage fences, and explicit cleanup."""

from __future__ import annotations

from dataclasses import dataclass

from theater.daemon.worktrees.identity import (
    ExistingPathFacts,
    WorktreeCreationFacts,
)
from theater.models import (
    TheaterError,
    WorkspaceRecord,
    WorkspaceUsageRecord,
)


class WorkspaceNotFound(TheaterError):
    code = "not_found"

    def __init__(self, workspace_id: str) -> None:
        self.details = {"workspace_id": workspace_id}
        super().__init__(f"no workspace {workspace_id!r} exists")


class WorkspaceInUse(TheaterError):
    code = "workspace_in_use"

    def __init__(self, workspace_id: str, usage_ids: tuple[str, ...]) -> None:
        self.details = {"workspace_id": workspace_id, "usage_ids": list(usage_ids)}
        super().__init__(f"workspace {workspace_id!r} has active usage and cannot be deleted")


class WorkspaceDeleting(TheaterError):
    code = "workspace_deleting"

    def __init__(self, workspace_id: str, state: str) -> None:
        self.details = {"workspace_id": workspace_id, "state": state}
        super().__init__(f"workspace {workspace_id!r} is {state} and cannot admit new usage")


class WorkspaceOwnershipConflict(TheaterError):
    code = "ownership_conflict"

    def __init__(self, workspace_id: str, reason: str) -> None:
        self.details = {"workspace_id": workspace_id, "reason": reason}
        super().__init__(f"workspace {workspace_id!r} ownership check failed: {reason}")


@dataclass(frozen=True, slots=True)
class WorkspaceRequest:
    workspace_id: str | None = None
    cwd: str | None = None
    worktree: bool | str = False
    base_ref: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceReservation:
    workspace: WorkspaceRecord
    usage: WorkspaceUsageRecord
    created: bool


@dataclass(frozen=True, slots=True)
class WorkspacePreparation:
    """Immutable Git facts collected before an operation claims a write unit."""

    request: WorkspaceRequest
    creation_facts: WorktreeCreationFacts | None = None
    existing_path_facts: ExistingPathFacts | None = None
    named_workspace: WorkspaceRecord | None = None
