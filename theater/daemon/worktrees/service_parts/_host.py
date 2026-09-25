"""Typing-only declaration of shared ``WorkspaceService`` state."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from sqlalchemy import Connection

    from theater.daemon.operations import OperationService
    from theater.daemon.persistence.store import Store
    from theater.daemon.worktrees.identity import ExistingPathFacts, WorktreeInspection
    from theater.daemon.worktrees.journal import WorkspaceJournal
    from theater.daemon.worktrees.service_parts._common import (
        WorkspaceRequest,
        WorkspaceReservation,
    )
    from theater.models import WorkspaceRecord

    class WorkspaceHost(Protocol):
        """The ``WorkspaceService`` surface one mixin may use from another."""

        _store: Store
        _journal: WorkspaceJournal
        _operations: OperationService
        _clock: Callable[[], float]
        _id_factory: Callable[[], str]
        _creation_workers: dict[str, int]

        def get(
            self, workspace_id: str, *, connection: Connection | None = None
        ) -> WorkspaceRecord: ...
        def project(
            self, record: WorkspaceRecord, *, connection: Connection | None = None
        ) -> dict[str, object]: ...
        async def materialize_creation(
            self, reservation: WorkspaceReservation, *, reservation_id: str
        ) -> WorkspaceReservation: ...
        async def _reconcile_creation_intent(self, workspace_id: str) -> bool: ...
        def _begin_delete(
            self,
            workspace: WorkspaceRecord,
            *,
            request_id: str,
            token: str,
            force: bool | None,
            delete_branch: bool | None,
            force_branch: bool | None,
            timestamp: float,
            connection: Connection,
            allowed_states: tuple[str, ...] = ...,
        ) -> None: ...
        @staticmethod
        def _create_from_intent(record: WorkspaceRecord) -> str: ...
        @staticmethod
        def _require_active(workspace: WorkspaceRecord) -> None: ...
        @staticmethod
        def _validate_registration_facts(
            params: Mapping[str, object], facts: ExistingPathFacts
        ) -> None: ...
        @staticmethod
        def _validate_request(request: WorkspaceRequest) -> None: ...
        @staticmethod
        def _validate_reuse(record: WorkspaceRecord, params: Mapping[str, object]) -> None: ...
        @staticmethod
        def _verify_created_base(actual: str, expected: str | None) -> None: ...
        @staticmethod
        def _verify_created_path(actual: str, expected: str) -> None: ...
        @staticmethod
        def _verify_named_workspace(record: WorkspaceRecord) -> WorktreeInspection: ...

else:
    WorkspaceHost = object
