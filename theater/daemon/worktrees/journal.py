"""Workspace wire projection and journal publication inside the caller's write unit."""

from sqlalchemy import Connection

from theater.daemon.events.publication import workspace_usage_event
from theater.daemon.persistence.transactions import WriteUnit
from theater.models import JournalEventRecord, WorkspaceRecord, WorkspaceUsageRecord


class WorkspaceJournal:
    def __init__(self, store) -> None:
        self._store = store

    def project(
        self, record: WorkspaceRecord, *, connection: Connection | None = None
    ) -> dict[str, object]:
        usages = self._store.workspaces.active_usages(record.workspace_id, connection=connection)
        fence = None
        if record.deletion_token is not None:
            fence = {
                "token": record.deletion_token,
                "revision": self._token_revision(record.deletion_token),
                "owner_id": record.owner_id,
                "operation_id": record.deletion_operation_id,
            }
        return {
            "workspace_id": record.workspace_id,
            "ownership_kind": record.ownership_kind,
            "owner_id": record.owner_id,
            "path": record.path,
            "canonical_repository_root": record.canonical_repository_root,
            "resolved_base_commit": record.resolved_base_commit,
            "branch": record.branch,
            "name": record.name,
            "state": record.state,
            "usages": [self._usage_to_wire(usage) for usage in usages],
            "deletion_fence": fence,
        }

    def append_workspace(
        self, unit: WriteUnit, record: WorkspaceRecord, *, revision: int | None = None
    ) -> None:
        self._store.journal.append_group(
            unit, [self.workspace_event(unit, record, revision=revision)]
        )

    def workspace_event(
        self,
        unit: WriteUnit,
        record: WorkspaceRecord,
        *,
        revision: int | None = None,
    ) -> JournalEventRecord:
        value = revision or self._store.journal.current_sequence(connection=unit.connection) + 1
        return JournalEventRecord(
            kind="workspace.updated",
            entity_id=record.workspace_id,
            entity_revision=value,
            payload=self.project(record, connection=unit.connection),
            recorded_at=record.updated_at,
        )

    def append_usage(self, unit: WriteUnit, usage: WorkspaceUsageRecord) -> None:
        revision = self._store.journal.current_sequence(connection=unit.connection) + 1
        self._store.journal.append_group(
            unit,
            [
                workspace_usage_event(
                    self._store,
                    usage,
                    unit.connection,
                    revision=revision,
                    recorded_at=(
                        usage.released_at if usage.released_at is not None else usage.acquired_at
                    ),
                )
            ],
        )

    def append_handoff(
        self,
        unit: WriteUnit,
        participant: WorkspaceUsageRecord,
    ) -> None:
        revision = self._store.journal.current_sequence(connection=unit.connection) + 1
        self._store.journal.append_group(
            unit,
            [
                workspace_usage_event(
                    self._store,
                    participant,
                    unit.connection,
                    revision=revision,
                    recorded_at=participant.acquired_at,
                )
            ],
        )

    def append_creation(
        self,
        unit: WriteUnit,
        *,
        workspace: WorkspaceRecord | None,
        usage: WorkspaceUsageRecord | None,
    ) -> None:
        if workspace is None and usage is None:
            return
        revision = self._store.journal.current_sequence(connection=unit.connection) + 1
        events: list[JournalEventRecord] = []
        if workspace is not None:
            events.append(
                JournalEventRecord(
                    kind="workspace.updated",
                    entity_id=workspace.workspace_id,
                    entity_revision=revision,
                    payload=self.project(workspace, connection=unit.connection),
                    recorded_at=workspace.updated_at,
                )
            )
            revision += 1
        if usage is not None:
            events.append(
                workspace_usage_event(
                    self._store,
                    usage,
                    unit.connection,
                    revision=revision,
                    recorded_at=usage.acquired_at,
                )
            )
        self._store.journal.append_group(unit, events)

    @staticmethod
    def _usage_to_wire(usage: WorkspaceUsageRecord) -> dict[str, object]:
        return {
            "usage_id": usage.usage_id,
            "workspace_id": usage.workspace_id,
            "holder_kind": usage.holder_kind,
            "holder_id": usage.holder_id,
            "acquired_at": usage.acquired_at,
            "released_at": usage.released_at,
            "release_reason": usage.release_reason,
        }

    @staticmethod
    def _token_revision(token: str) -> int:
        try:
            revision = int(token.split("-", 1)[0])
        except ValueError:
            return 0
        return max(revision, 0)
