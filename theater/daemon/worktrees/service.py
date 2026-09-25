"""Durable workspace lifecycle composed from focused concern mixins."""

from __future__ import annotations

from collections.abc import Callable

from theater.daemon.operations import OperationService
from theater.daemon.worktrees.journal import WorkspaceJournal
from theater.daemon.worktrees.service_parts._common import (
    WorkspaceDeleting,
    WorkspaceInUse,
    WorkspaceNotFound,
    WorkspaceOwnershipConflict,
    WorkspacePreparation,
    WorkspaceRequest,
    WorkspaceReservation,
)
from theater.daemon.worktrees.service_parts.cleanup_service import WorkspaceCleanup
from theater.daemon.worktrees.service_parts.creation import WorkspaceCreation
from theater.daemon.worktrees.service_parts.recovery import WorkspaceRecovery
from theater.daemon.worktrees.service_parts.registration import WorkspaceRegistration
from theater.daemon.worktrees.service_parts.reservation import WorkspaceReservationFlow
from theater.daemon.worktrees.service_parts.usage import WorkspaceUsage
from theater.daemon.worktrees.service_parts.validation import WorkspaceValidation
from theater.models import new_id, now


class WorkspaceService(
    WorkspaceRegistration,
    WorkspaceReservationFlow,
    WorkspaceCreation,
    WorkspaceUsage,
    WorkspaceRecovery,
    WorkspaceCleanup,
    WorkspaceValidation,
):
    def __init__(
        self,
        store,
        operations: OperationService,
        *,
        clock: Callable[[], float] = now,
        id_factory: Callable[[], str] = new_id,
    ) -> None:
        self._store = store
        self._journal = WorkspaceJournal(store)
        self._operations = operations
        self._clock = clock
        self._id_factory = id_factory
        self._creation_workers: dict[str, int] = {}


__all__ = [
    "WorkspaceDeleting",
    "WorkspaceInUse",
    "WorkspaceNotFound",
    "WorkspaceOwnershipConflict",
    "WorkspacePreparation",
    "WorkspaceRequest",
    "WorkspaceReservation",
    "WorkspaceService",
]
