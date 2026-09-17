"""Exact workspace ownership, usage, handoff, and deletion fences."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import Connection, insert, select, update

from theater.daemon.persistence.database import Database
from theater.daemon.schema import workspace_usages, workspaces
from theater.models import WorkspaceRecord, WorkspaceUsageRecord


class WorkspaceRepository:
    def __init__(self, db: Database):
        self._db = db

    def create(self, record: WorkspaceRecord, *, connection: Connection) -> None:
        connection.execute(
            insert(workspaces).values(
                workspace_id=record.workspace_id,
                ownership_kind=record.ownership_kind,
                owner_id=record.owner_id,
                path=record.path,
                canonical_repository_root=record.canonical_repository_root,
                branch=record.branch,
                resolved_base_commit=record.resolved_base_commit,
                name=record.name,
                state=record.state,
                deletion_operation_id=record.deletion_operation_id,
                deletion_token=record.deletion_token,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
        )

    def get(
        self, workspace_id: str, *, connection: Connection | None = None
    ) -> WorkspaceRecord | None:
        conn = self._db.conn if connection is None else connection
        row = conn.execute(
            select(workspaces).where(workspaces.c.workspace_id == workspace_id)
        ).first()
        return self._workspace_from_row(dict(row._mapping)) if row else None

    def acquire_usage(self, usage: WorkspaceUsageRecord, *, connection: Connection) -> None:
        connection.execute(
            insert(workspace_usages).values(
                usage_id=usage.usage_id,
                workspace_id=usage.workspace_id,
                holder_kind=usage.holder_kind,
                holder_id=usage.holder_id,
                acquired_at=usage.acquired_at,
                released_at=usage.released_at,
                release_reason=usage.release_reason,
            )
        )

    def handoff_usage(
        self,
        *,
        reservation_usage_id: str,
        participant_usage: WorkspaceUsageRecord,
        handed_off_at: float,
        connection: Connection,
    ) -> None:
        released = connection.execute(
            update(workspace_usages)
            .where(workspace_usages.c.usage_id == reservation_usage_id)
            .where(workspace_usages.c.holder_kind == "reservation")
            .where(workspace_usages.c.released_at.is_(None))
            .values(released_at=handed_off_at, release_reason="participant_handoff")
        )
        if released.rowcount != 1:
            raise KeyError(f"active reservation usage {reservation_usage_id!r} was not found")
        self.acquire_usage(participant_usage, connection=connection)

    def active_usages(
        self, workspace_id: str, *, connection: Connection | None = None
    ) -> list[WorkspaceUsageRecord]:
        conn = self._db.conn if connection is None else connection
        rows = conn.execute(
            select(workspace_usages)
            .where(workspace_usages.c.workspace_id == workspace_id)
            .where(workspace_usages.c.released_at.is_(None))
            .order_by(workspace_usages.c.acquired_at, workspace_usages.c.usage_id)
        ).fetchall()
        return [self._usage_from_row(dict(row._mapping)) for row in rows]

    def prepare_delete(
        self,
        workspace_id: str,
        *,
        operation_id: str,
        token: str,
        updated_at: float,
        connection: Connection,
    ) -> bool:
        active = connection.execute(
            select(workspace_usages.c.usage_id)
            .where(workspace_usages.c.workspace_id == workspace_id)
            .where(workspace_usages.c.released_at.is_(None))
            .limit(1)
        ).first()
        if active is not None:
            return False
        updated = connection.execute(
            update(workspaces)
            .where(workspaces.c.workspace_id == workspace_id)
            .where(workspaces.c.state == "active")
            .values(
                state="deleting",
                deletion_operation_id=operation_id,
                deletion_token=token,
                updated_at=updated_at,
            )
        )
        return bool(updated.rowcount)

    @staticmethod
    def _workspace_from_row(row: Mapping[str, Any]) -> WorkspaceRecord:
        return WorkspaceRecord(
            workspace_id=str(row["workspace_id"]),
            ownership_kind=str(row["ownership_kind"]),
            owner_id=str(row["owner_id"]),
            path=str(row["path"]),
            canonical_repository_root=_optional_str(row["canonical_repository_root"]),
            branch=_optional_str(row["branch"]),
            resolved_base_commit=_optional_str(row["resolved_base_commit"]),
            name=_optional_str(row["name"]),
            state=str(row["state"]),
            deletion_operation_id=_optional_str(row["deletion_operation_id"]),
            deletion_token=_optional_str(row["deletion_token"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _usage_from_row(row: Mapping[str, Any]) -> WorkspaceUsageRecord:
        return WorkspaceUsageRecord(
            usage_id=str(row["usage_id"]),
            workspace_id=str(row["workspace_id"]),
            holder_kind=str(row["holder_kind"]),
            holder_id=str(row["holder_id"]),
            acquired_at=float(row["acquired_at"]),
            released_at=None if row["released_at"] is None else float(row["released_at"]),
            release_reason=_optional_str(row["release_reason"]),
        )


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


__all__ = ["WorkspaceRepository"]
