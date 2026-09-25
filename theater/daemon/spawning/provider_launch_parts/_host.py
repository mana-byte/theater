"""Typing-only declaration of shared ``ParticipantLaunchService`` state."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy import Connection

    from theater.daemon.operations import OperationAcceptance, OperationService
    from theater.daemon.persistence.store import Store
    from theater.daemon.persistence.transactions import WriteUnit
    from theater.daemon.registry import Registry
    from theater.daemon.server import Daemon
    from theater.daemon.spawning.models import SpawnRequest
    from theater.daemon.spawning.service import Spawner
    from theater.daemon.terminals.service import TerminalProviderService
    from theater.daemon.worktrees.service import WorkspaceRequest, WorkspaceService
    from theater.models import (
        Job,
        JournalEventRecord,
        Participant,
        PublicOperationRecord,
        TerminalBindingRecord,
        WorkspaceUsageRecord,
    )

    class ParticipantLaunchHost(Protocol):
        """The launch-service surface one mixin may use from another."""

        DEFAULT_PROVIDER_SELECTOR: str
        daemon: Daemon
        store: Store
        registry: Registry
        spawner: Spawner
        operations: OperationService
        terminals: TerminalProviderService
        workspaces: WorkspaceService
        default_provider_selector: str

        def _append_binding_events(
            self,
            unit: WriteUnit,
            participant: Participant,
            binding: TerminalBindingRecord,
            usage: WorkspaceUsageRecord | None,
            *,
            operation: PublicOperationRecord,
            completed_job: Job | None = None,
        ) -> None: ...
        @staticmethod
        def _binding(
            participant_id: str, terminal: Mapping[str, object]
        ) -> TerminalBindingRecord: ...
        def _clear_provider_dispatch_target(
            self, operation_id: str, unit: WriteUnit, *, timestamp: float
        ) -> PublicOperationRecord: ...
        def _ensure_terminal_unbound(
            self, candidate: TerminalBindingRecord, connection: Connection
        ) -> None: ...
        @staticmethod
        def _job_event(job: Job, timestamp: float, *, revision: int) -> JournalEventRecord: ...
        @staticmethod
        def _optional_text(value: object) -> str | None: ...
        def _participant_event(
            self,
            participant: Participant,
            timestamp: float,
            *,
            revision: int,
            connection: Connection,
        ) -> JournalEventRecord: ...
        @staticmethod
        def _participant_in_connection(
            participant_id: str, connection: Connection
        ) -> Participant | None: ...
        def _persist_dispatch_identity(
            self, operation_id: str, terminal: Mapping[str, object], unit: WriteUnit
        ) -> PublicOperationRecord: ...
        @staticmethod
        def _resolve_binary(name: str) -> str | None: ...
        def _rollback_adoption_reservation(self, participant_id: str) -> None: ...
        async def _rollback_spawn_reservation(
            self,
            operation_id: str,
            participant_id: str,
            workspace_value: object,
            *,
            error_code: str,
            definitive_refusal: bool = False,
        ) -> None: ...
        def _select_provider(
            self, requested: object, unit: WriteUnit, *, allow_selector: bool = True
        ) -> tuple[str, int]: ...
        @staticmethod
        def _spawn_request(
            params: Mapping[str, object],
            *,
            parent_id: str | None,
            cwd: str,
            worktree: bool | str,
            base_ref: str | None,
        ) -> SpawnRequest: ...
        def _start_spawn(
            self,
            acceptance: OperationAcceptance,
            captured: dict[str, object],
            params: Mapping[str, object],
            participant: Participant,
        ) -> None: ...
        @staticmethod
        def _validate_workspace_request(request: WorkspaceRequest) -> None: ...
        @staticmethod
        def _workspace_request(params: Mapping[str, object]) -> WorkspaceRequest: ...
        @staticmethod
        def _workspace_request_facts(request: WorkspaceRequest) -> dict[str, object]: ...
        def _workspace_usage_event(
            self,
            usage: WorkspaceUsageRecord,
            timestamp: float,
            *,
            revision: int,
            connection: Connection,
        ) -> JournalEventRecord: ...

else:
    ParticipantLaunchHost = object
