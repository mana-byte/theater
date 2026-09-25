"""Provider selection, request validation, and event projection."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select

from theater.daemon.events.publication import (
    job_event,
    participant_event,
    workspace_usage_event,
)
from theater.daemon.persistence.transactions import WriteUnit
from theater.daemon.schema import participants
from theater.daemon.spawning.provider_launch_parts._host import ParticipantLaunchHost
from theater.daemon.terminals import ProviderUnavailable
from theater.daemon.worktrees.service import (
    WorkspaceRequest,
)
from theater.frontend.capabilities import TERMINAL_PROVIDER_CAPABILITY
from theater.models import (
    BadRequest,
    Job,
    JournalEventRecord,
    Participant,
    WorkspaceUsageRecord,
)


class LaunchValidation(ParticipantLaunchHost):
    def _select_provider(
        self, requested: object, unit: WriteUnit, *, allow_selector: bool = True
    ) -> tuple[str, int]:
        token = (
            requested
            if isinstance(requested, str) and requested
            else self.default_provider_selector
        )
        record = self.store.providers.get(token, connection=unit.connection)
        if record is None and allow_selector:
            record = self.store.providers.get_by_selector(token, connection=unit.connection)
        if record is None:
            raise ProviderUnavailable(token, "not_registered")
        if TERMINAL_PROVIDER_CAPABILITY not in record.capabilities:
            raise ProviderUnavailable(record.provider_id, "missing_terminal_create_capability")
        if self.terminals.connections.health(record.provider_id) != "online":
            raise ProviderUnavailable(record.provider_id, "not_launchable")
        if not self.terminals.connections.is_current(record.provider_id, record.generation):
            raise ProviderUnavailable(record.provider_id, "no_current_callback_generation")
        return record.provider_id, record.generation

    @staticmethod
    def _participant_in_connection(participant_id: str, connection) -> Participant | None:
        row = connection.execute(
            select(participants).where(participants.c.id == participant_id)
        ).first()
        return Participant.from_row(row._mapping) if row is not None else None

    @staticmethod
    def _workspace_request(params: Mapping[str, object]) -> WorkspaceRequest:
        raw = params.get("workspace")
        values = raw if isinstance(raw, Mapping) else {}
        workspace_id = values.get("workspace_id")
        cwd = values.get("cwd", params.get("cwd"))
        worktree = values.get("worktree", False)
        base_ref = values.get("base_ref")
        if workspace_id is not None and not isinstance(workspace_id, str):
            raise BadRequest("workspace_id must be a string")
        if cwd is not None and not isinstance(cwd, str):
            raise BadRequest("workspace cwd must be a string")
        if not isinstance(worktree, (bool, str)):
            raise BadRequest("workspace worktree must be a boolean or name")
        if base_ref is not None and not isinstance(base_ref, str):
            raise BadRequest("workspace base_ref must be a string")
        return WorkspaceRequest(
            workspace_id=workspace_id,
            cwd=cwd,
            worktree=worktree,
            base_ref=base_ref,
        )

    @staticmethod
    def _workspace_request_facts(request: WorkspaceRequest) -> dict[str, object]:
        return {
            "workspace_id": request.workspace_id,
            "cwd": request.cwd,
            "worktree": request.worktree,
            "base_ref": request.base_ref,
        }

    @staticmethod
    def _validate_workspace_request(request: WorkspaceRequest) -> None:
        if request.workspace_id is not None:
            if (
                request.cwd is not None
                or request.worktree is not False
                or request.base_ref is not None
            ):
                raise BadRequest("workspace_id cannot be combined with cwd, worktree, or base_ref")
            return
        if request.cwd is None:
            raise BadRequest("workspace preparation requires workspace_id or cwd")
        if not isinstance(request.worktree, (bool, str)) or request.worktree == "":
            raise BadRequest("worktree must be false, true, or a non-empty name")
        if request.base_ref is not None and request.worktree is False:
            raise BadRequest("base_ref requires unique or named worktree creation")

    def _participant_event(
        self,
        participant: Participant,
        timestamp: float,
        *,
        revision: int,
        connection,
    ) -> JournalEventRecord:
        return participant_event(
            self.store,
            participant,
            connection,
            revision=revision,
            recorded_at=timestamp,
        )

    @staticmethod
    def _job_event(job: Job, timestamp: float, *, revision: int) -> JournalEventRecord:
        return job_event(job, revision=revision, recorded_at=timestamp)

    def _workspace_usage_event(
        self,
        usage: WorkspaceUsageRecord,
        timestamp: float,
        *,
        revision: int,
        connection,
    ) -> JournalEventRecord:
        return workspace_usage_event(
            self.store,
            usage,
            connection,
            revision=revision,
            recorded_at=timestamp,
        )
