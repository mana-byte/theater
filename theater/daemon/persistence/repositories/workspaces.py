"""Exact workspace ownership, usage, handoff, and deletion fences."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import Connection, exists, insert, literal, or_, select, update

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
                creation_operation_id=record.creation_operation_id,
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

    def get_active_by_path(
        self, path: str, *, connection: Connection | None = None
    ) -> WorkspaceRecord | None:
        conn = self._db.conn if connection is None else connection
        row = conn.execute(
            select(workspaces)
            .where(workspaces.c.path == path, workspaces.c.state != "removed")
            .order_by(workspaces.c.created_at.desc(), workspaces.c.workspace_id.desc())
            .limit(1)
        ).first()
        return self._workspace_from_row(dict(row._mapping)) if row else None

    def get_active_named(
        self,
        canonical_repository_root: str,
        name: str,
        *,
        connection: Connection | None = None,
    ) -> WorkspaceRecord | None:
        conn = self._db.conn if connection is None else connection
        row = conn.execute(
            select(workspaces)
            .where(
                workspaces.c.canonical_repository_root == canonical_repository_root,
                workspaces.c.name == name,
                workspaces.c.state != "removed",
            )
            .order_by(workspaces.c.created_at.desc(), workspaces.c.workspace_id.desc())
            .limit(1)
        ).first()
        return self._workspace_from_row(dict(row._mapping)) if row else None

    def list_page(
        self,
        *,
        cursor: str | None,
        limit: int,
        state: str | None = None,
        connection: Connection | None = None,
    ) -> tuple[tuple[WorkspaceRecord, ...], str | None]:
        conn = self._db.conn if connection is None else connection
        query = select(workspaces)
        if cursor is not None:
            cursor_row = conn.execute(
                select(workspaces.c.created_at, workspaces.c.workspace_id).where(
                    workspaces.c.workspace_id == cursor
                )
            ).first()
            if cursor_row is None:
                raise KeyError(cursor)
            created_at, workspace_id = cursor_row
            query = query.where(
                or_(
                    workspaces.c.created_at < created_at,
                    (workspaces.c.created_at == created_at)
                    & (workspaces.c.workspace_id < workspace_id),
                )
            )
        if state is not None:
            query = query.where(workspaces.c.state == state)
        rows = conn.execute(
            query.order_by(workspaces.c.created_at.desc(), workspaces.c.workspace_id.desc()).limit(
                limit + 1
            )
        ).all()
        records = tuple(self._workspace_from_row(dict(row._mapping)) for row in rows[:limit])
        next_cursor = records[-1].workspace_id if len(rows) > limit else None
        return records, next_cursor

    def list_by_states(
        self,
        states: tuple[str, ...],
        *,
        limit: int,
        connection: Connection | None = None,
    ) -> tuple[WorkspaceRecord, ...]:
        conn = self._db.conn if connection is None else connection
        rows = conn.execute(
            select(workspaces)
            .where(workspaces.c.state.in_(states))
            .order_by(workspaces.c.updated_at, workspaces.c.workspace_id)
            .limit(limit)
        ).all()
        return tuple(self._workspace_from_row(dict(row._mapping)) for row in rows)

    def list_by_states_page(
        self,
        states: tuple[str, ...],
        *,
        cursor: tuple[float, str] | None,
        limit: int,
        connection: Connection | None = None,
    ) -> tuple[tuple[WorkspaceRecord, ...], tuple[float, str] | None]:
        """Scan pending lifecycle rows without repeatedly pinning the oldest page."""
        conn = self._db.conn if connection is None else connection
        query = select(workspaces).where(workspaces.c.state.in_(states))
        if cursor is not None:
            updated_at, workspace_id = cursor
            query = query.where(
                or_(
                    workspaces.c.updated_at > updated_at,
                    (workspaces.c.updated_at == updated_at)
                    & (workspaces.c.workspace_id > workspace_id),
                )
            )
        rows = conn.execute(
            query.order_by(workspaces.c.updated_at, workspaces.c.workspace_id).limit(limit + 1)
        ).all()
        records = tuple(self._workspace_from_row(dict(row._mapping)) for row in rows[:limit])
        next_cursor = (
            (records[-1].updated_at, records[-1].workspace_id) if len(rows) > limit else None
        )
        return records, next_cursor

    def mark_creation_reconcile(
        self,
        workspace_id: str,
        *,
        operation_id: str,
        updated_at: float,
        connection: Connection,
    ) -> bool:
        updated = connection.execute(
            update(workspaces)
            .where(
                workspaces.c.workspace_id == workspace_id,
                workspaces.c.state == "creating",
                workspaces.c.creation_operation_id == operation_id,
            )
            .values(state="reconcile", updated_at=updated_at)
        )
        return bool(updated.rowcount)

    def mark_creation_removed(
        self,
        workspace_id: str,
        *,
        operation_id: str,
        updated_at: float,
        connection: Connection,
    ) -> bool:
        updated = connection.execute(
            update(workspaces)
            .where(
                workspaces.c.workspace_id == workspace_id,
                workspaces.c.state == "creating",
                workspaces.c.creation_operation_id == operation_id,
            )
            .values(state="removed", updated_at=updated_at)
        )
        return bool(updated.rowcount)

    def acquire_usage(self, usage: WorkspaceUsageRecord, *, connection: Connection) -> bool:
        values = select(
            literal(usage.usage_id),
            literal(usage.workspace_id),
            literal(usage.holder_kind),
            literal(usage.holder_id),
            literal(usage.acquired_at),
            literal(usage.released_at),
            literal(usage.release_reason),
        ).where(
            exists(
                select(workspaces.c.workspace_id).where(
                    workspaces.c.workspace_id == usage.workspace_id,
                    workspaces.c.state == "active",
                )
            )
        )
        inserted = connection.execute(
            insert(workspace_usages).from_select(
                (
                    workspace_usages.c.usage_id,
                    workspace_usages.c.workspace_id,
                    workspace_usages.c.holder_kind,
                    workspace_usages.c.holder_id,
                    workspace_usages.c.acquired_at,
                    workspace_usages.c.released_at,
                    workspace_usages.c.release_reason,
                ),
                values,
            )
        )
        return bool(inserted.rowcount)

    def acquire_creation_usage(
        self, usage: WorkspaceUsageRecord, *, connection: Connection
    ) -> bool:
        """Acquire the one reservation that owns a durable creation intent."""
        if usage.holder_kind != "reservation":
            raise ValueError("only a creation reservation may use a creating workspace")
        values = select(
            literal(usage.usage_id),
            literal(usage.workspace_id),
            literal(usage.holder_kind),
            literal(usage.holder_id),
            literal(usage.acquired_at),
            literal(usage.released_at),
            literal(usage.release_reason),
        ).where(
            exists(
                select(workspaces.c.workspace_id).where(
                    workspaces.c.workspace_id == usage.workspace_id,
                    workspaces.c.state == "creating",
                    workspaces.c.creation_operation_id == usage.holder_id,
                )
            )
        )
        inserted = connection.execute(
            insert(workspace_usages).from_select(
                (
                    workspace_usages.c.usage_id,
                    workspace_usages.c.workspace_id,
                    workspace_usages.c.holder_kind,
                    workspace_usages.c.holder_id,
                    workspace_usages.c.acquired_at,
                    workspace_usages.c.released_at,
                    workspace_usages.c.release_reason,
                ),
                values,
            )
        )
        return bool(inserted.rowcount)

    def mark_creation_ready(
        self,
        workspace_id: str,
        *,
        operation_id: str,
        updated_at: float,
        connection: Connection,
    ) -> bool:
        updated = connection.execute(
            update(workspaces)
            .where(
                workspaces.c.workspace_id == workspace_id,
                workspaces.c.state == "creating",
                workspaces.c.creation_operation_id == operation_id,
            )
            .values(state="active", updated_at=updated_at)
        )
        return bool(updated.rowcount)

    def settle_reconcile_workspace(
        self,
        workspace_id: str,
        *,
        state: str,
        resolved_base_commit: str | None,
        updated_at: float,
        connection: Connection,
    ) -> bool:
        updated = connection.execute(
            update(workspaces)
            .where(
                workspaces.c.workspace_id == workspace_id,
                workspaces.c.state == "reconcile",
            )
            .values(
                state=state,
                resolved_base_commit=resolved_base_commit,
                updated_at=updated_at,
            )
        )
        return bool(updated.rowcount)

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
        if not self.acquire_usage(participant_usage, connection=connection):
            raise KeyError(f"active workspace {participant_usage.workspace_id!r} was not found")

    def release_usage(
        self,
        usage_id: str,
        *,
        released_at: float,
        reason: str,
        connection: Connection,
    ) -> bool:
        released = connection.execute(
            update(workspace_usages)
            .where(
                workspace_usages.c.usage_id == usage_id,
                workspace_usages.c.released_at.is_(None),
            )
            .values(released_at=released_at, release_reason=reason)
        )
        return bool(released.rowcount)

    def get_usage(
        self, usage_id: str, *, connection: Connection | None = None
    ) -> WorkspaceUsageRecord | None:
        conn = self._db.conn if connection is None else connection
        row = conn.execute(
            select(workspace_usages).where(workspace_usages.c.usage_id == usage_id)
        ).first()
        return self._usage_from_row(dict(row._mapping)) if row else None

    def get_active_usage(
        self,
        workspace_id: str,
        *,
        holder_kind: str,
        holder_id: str,
        connection: Connection | None = None,
    ) -> WorkspaceUsageRecord | None:
        conn = self._db.conn if connection is None else connection
        row = conn.execute(
            select(workspace_usages).where(
                workspace_usages.c.workspace_id == workspace_id,
                workspace_usages.c.holder_kind == holder_kind,
                workspace_usages.c.holder_id == holder_id,
                workspace_usages.c.released_at.is_(None),
            )
        ).first()
        return self._usage_from_row(dict(row._mapping)) if row else None

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
        allowed_states: tuple[str, ...] = ("active",),
    ) -> bool:
        updated = connection.execute(
            update(workspaces)
            .where(workspaces.c.workspace_id == workspace_id)
            .where(workspaces.c.state.in_(allowed_states))
            .where(
                ~exists(
                    select(workspace_usages.c.usage_id).where(
                        workspace_usages.c.workspace_id == workspace_id,
                        workspace_usages.c.released_at.is_(None),
                    )
                )
            )
            .values(
                state="deleting",
                deletion_operation_id=operation_id,
                deletion_token=token,
                updated_at=updated_at,
            )
        )
        return bool(updated.rowcount)

    def finish_delete(
        self,
        workspace_id: str,
        *,
        operation_id: str,
        token: str,
        state: str,
        updated_at: float,
        connection: Connection,
    ) -> bool:
        updated = connection.execute(
            update(workspaces)
            .where(
                workspaces.c.workspace_id == workspace_id,
                workspaces.c.state == "deleting",
                workspaces.c.deletion_operation_id == operation_id,
                workspaces.c.deletion_token == token,
            )
            .values(
                state=state,
                deletion_operation_id=None,
                deletion_token=None,
                updated_at=updated_at,
            )
        )
        return bool(updated.rowcount)

    def resolve_external_delete(
        self,
        workspace_id: str,
        *,
        token: str,
        state: str,
        updated_at: float,
        connection: Connection,
    ) -> bool:
        updated = connection.execute(
            update(workspaces)
            .where(
                workspaces.c.workspace_id == workspace_id,
                workspaces.c.state == "deleting",
                workspaces.c.deletion_token == token,
            )
            .values(
                state=state,
                deletion_operation_id=None,
                deletion_token=None,
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
            creation_operation_id=_optional_str(row["creation_operation_id"]),
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
