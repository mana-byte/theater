"""Workspace usage handoff and release."""

from __future__ import annotations

from theater.daemon.worktrees.service_parts._host import WorkspaceHost
from theater.models import (
    WorkspaceUsageHolderKind,
    WorkspaceUsageRecord,
)


class WorkspaceUsage(WorkspaceHost):
    def handoff_usage(
        self, reservation_usage_id: str, *, participant_id: str
    ) -> WorkspaceUsageRecord:
        timestamp = self._clock()
        with self._store.write_unit() as unit:
            reservation = self._store.workspaces.get_usage(
                reservation_usage_id, connection=unit.connection
            )
            if reservation is None or reservation.holder_kind != "reservation":
                raise KeyError(f"reservation usage {reservation_usage_id!r} was not found")
            workspace = self.get(reservation.workspace_id, connection=unit.connection)
            self._require_active(workspace)
            existing = self._store.workspaces.get_active_usage(
                workspace.workspace_id,
                holder_kind=WorkspaceUsageHolderKind.PARTICIPANT.value,
                holder_id=participant_id,
                connection=unit.connection,
            )
            if existing is not None and reservation.released_at is not None:
                return existing
            usage = WorkspaceUsageRecord(
                usage_id=self._id_factory(),
                workspace_id=workspace.workspace_id,
                holder_kind=WorkspaceUsageHolderKind.PARTICIPANT.value,
                holder_id=participant_id,
                acquired_at=timestamp,
            )
            self._store.workspaces.handoff_usage(
                reservation_usage_id=reservation_usage_id,
                participant_usage=usage,
                handed_off_at=timestamp,
                connection=unit.connection,
            )
            self._journal.append_handoff(unit, usage)
        return usage

    def release_usage(self, usage_id: str, *, reason: str) -> WorkspaceUsageRecord:
        if not reason or len(reason) > 512:
            raise ValueError("workspace release reason must contain 1 to 512 characters")
        timestamp = self._clock()
        with self._store.write_unit() as unit:
            usage = self._store.workspaces.get_usage(usage_id, connection=unit.connection)
            if usage is None:
                raise KeyError(f"workspace usage {usage_id!r} was not found")
            if usage.released_at is not None:
                return usage
            if not self._store.workspaces.release_usage(
                usage_id,
                released_at=timestamp,
                reason=reason,
                connection=unit.connection,
            ):
                raise RuntimeError("workspace usage changed during its write unit")
            released = WorkspaceUsageRecord(
                usage_id=usage.usage_id,
                workspace_id=usage.workspace_id,
                holder_kind=usage.holder_kind,
                holder_id=usage.holder_id,
                acquired_at=usage.acquired_at,
                released_at=timestamp,
                release_reason=reason,
            )
            self._journal.append_usage(unit, released)
        return released

    def release_participant_usage(
        self,
        *,
        workspace_id: str,
        participant_id: str,
        reason: str,
    ) -> WorkspaceUsageRecord | None:
        """Release a held participant usage only after its exit is confirmed."""
        with self._store.write_unit() as unit:
            usage = self._store.workspaces.get_active_usage(
                workspace_id,
                holder_kind=WorkspaceUsageHolderKind.PARTICIPANT.value,
                holder_id=participant_id,
                connection=unit.connection,
            )
            if usage is None:
                return None
            timestamp = self._clock()
            if not self._store.workspaces.release_usage(
                usage.usage_id,
                released_at=timestamp,
                reason=reason,
                connection=unit.connection,
            ):
                raise RuntimeError("workspace usage changed during its write unit")
            released = WorkspaceUsageRecord(
                usage_id=usage.usage_id,
                workspace_id=usage.workspace_id,
                holder_kind=usage.holder_kind,
                holder_id=usage.holder_id,
                acquired_at=usage.acquired_at,
                released_at=timestamp,
                release_reason=reason,
            )
            self._journal.append_usage(unit, released)
        return released
