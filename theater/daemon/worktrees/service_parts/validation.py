"""Workspace request and ownership validation."""

from __future__ import annotations

from collections.abc import Mapping

from theater.daemon.worktrees.identity import (
    ExistingPathFacts,
    GitFactsError,
)
from theater.daemon.worktrees.service_parts._common import (
    WorkspaceDeleting,
    WorkspaceOwnershipConflict,
    WorkspaceRequest,
)
from theater.daemon.worktrees.service_parts._host import WorkspaceHost
from theater.models import (
    WorkspaceOwnershipKind,
    WorkspaceRecord,
    WorkspaceState,
)


class WorkspaceValidation(WorkspaceHost):
    @staticmethod
    def _validate_registration_facts(
        params: Mapping[str, object], facts: ExistingPathFacts
    ) -> None:
        if params["ownership_kind"] not in {
            WorkspaceOwnershipKind.FRONTEND.value,
            WorkspaceOwnershipKind.BORROWED.value,
        }:
            raise WorkspaceOwnershipConflict("unregistered", "invalid_external_owner")
        expected = {
            "canonical_repository_root": facts.canonical_repository_root,
            "branch": facts.branch,
            "resolved_base_commit": facts.head_commit,
        }
        for key, value in expected.items():
            if key in params and params[key] != value:
                raise GitFactsError(f"supplied {key} does not match the existing directory")

    @staticmethod
    def _validate_reuse(record: WorkspaceRecord, params: Mapping[str, object]) -> None:
        if record.state != WorkspaceState.ACTIVE.value:
            raise WorkspaceDeleting(record.workspace_id, record.state)
        if (
            record.ownership_kind != params["ownership_kind"]
            or record.owner_id != params["owner_id"]
        ):
            raise WorkspaceOwnershipConflict(record.workspace_id, "path_already_owned")

    @staticmethod
    def _validate_request(request: WorkspaceRequest) -> None:
        if request.workspace_id is not None:
            if (
                request.cwd is not None
                or request.worktree is not False
                or request.base_ref is not None
            ):
                raise ValueError("workspace_id cannot be combined with cwd, worktree, or base_ref")
            return
        if request.cwd is None:
            raise ValueError("workspace preparation requires workspace_id or cwd")
        if not isinstance(request.worktree, (bool, str)) or request.worktree == "":
            raise ValueError("worktree must be false, true, or a non-empty name")
        if request.base_ref is not None and request.worktree is False:
            raise ValueError("base_ref requires unique or named worktree creation")

    @staticmethod
    def _require_active(workspace: WorkspaceRecord) -> None:
        if workspace.state != WorkspaceState.ACTIVE.value:
            raise WorkspaceDeleting(workspace.workspace_id, workspace.state)
