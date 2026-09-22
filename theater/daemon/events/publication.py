"""Canonical public projections for transactional journal writers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from sqlalchemy import Connection

from theater.daemon.events.snapshot import (
    _job_projection,
    _participant_projection,
    _workspace_projection,
)
from theater.models import (
    Job,
    JournalEventRecord,
    Participant,
    TerminalBindingRecord,
    WorkspaceRecord,
    WorkspaceUsageRecord,
)


def control_event(
    store,
    operation,
    connection: Connection,
    *,
    revision: int,
    projected_online_provider: tuple[str, int] | None = None,
) -> JournalEventRecord | None:
    participant = store.get_participant(operation.participant_id, connection=connection)
    if participant is None:
        return None
    return participant_event(
        store,
        participant,
        connection,
        revision=revision,
        recorded_at=operation.updated_at,
        kind="participant.controls_changed",
        projected_online_provider=projected_online_provider,
    )


def next_revision(store, connection: Connection, *, offset: int = 0) -> int:
    return store.journal.current_sequence(connection=connection) + offset + 1


def participant_event(
    store,
    participant: Participant,
    connection: Connection,
    *,
    revision: int,
    recorded_at: float,
    kind: Literal[
        "participant.updated",
        "participant.controls_changed",
        "participant.owner_changed",
        "terminal.binding_changed",
    ] = "participant.updated",
    extra: Mapping[str, object] | None = None,
    projected_online_provider: tuple[str, int] | None = None,
) -> JournalEventRecord:
    name = participant.name
    resolver = getattr(store, "participant_projection_name", None)
    if name is None and resolver is not None:
        name = resolver(participant.id)
    payload = _participant_projection(
        store,
        participant,
        connection,
        name=name,
        transactional=True,
        projected_online_provider=projected_online_provider,
    )
    if extra is not None:
        payload.update(extra)
    return JournalEventRecord(
        kind=kind,
        entity_id=participant.id,
        entity_revision=revision,
        payload=payload,
        recorded_at=recorded_at,
    )


def job_event(job: Job, *, revision: int, recorded_at: float) -> JournalEventRecord:
    return JournalEventRecord(
        kind="job.updated",
        entity_id=job.handle,
        entity_revision=revision,
        payload=_job_projection(job),
        recorded_at=recorded_at,
    )


def workspace_event(
    store,
    workspace: WorkspaceRecord,
    connection: Connection,
    *,
    revision: int,
    recorded_at: float,
) -> JournalEventRecord:
    return JournalEventRecord(
        kind="workspace.updated",
        entity_id=workspace.workspace_id,
        entity_revision=revision,
        payload=_workspace_projection(store, workspace, connection),
        recorded_at=recorded_at,
    )


def terminal_binding_event(
    store,
    binding: TerminalBindingRecord,
    connection: Connection,
    *,
    revision: int,
    recorded_at: float,
    projected_online_provider: tuple[str, int] | None = None,
) -> JournalEventRecord:
    participant = store.get_participant(binding.participant_id, connection=connection)
    if participant is None:
        raise RuntimeError("terminal binding has no participant projection")
    return participant_event(
        store,
        participant,
        connection,
        revision=revision,
        recorded_at=recorded_at,
        kind="terminal.binding_changed",
        projected_online_provider=projected_online_provider,
    )


def workspace_usage_event(
    store,
    usage: WorkspaceUsageRecord,
    connection: Connection,
    *,
    revision: int,
    recorded_at: float,
) -> JournalEventRecord:
    workspace = store.workspaces.get(usage.workspace_id, connection=connection)
    if workspace is None:
        raise RuntimeError("workspace usage has no workspace projection")
    return JournalEventRecord(
        kind="workspace.usage_changed",
        entity_id=usage.workspace_id,
        entity_revision=revision,
        payload=_workspace_projection(store, workspace, connection),
        recorded_at=recorded_at,
    )


def tombstone_event(
    kind: Literal["participant.removed", "job.removed"],
    entity_id: str,
    *,
    revision: int,
    recorded_at: float,
) -> JournalEventRecord:
    field = "participant_id" if kind == "participant.removed" else "handle"
    return JournalEventRecord(
        kind=kind,
        entity_id=entity_id,
        entity_revision=revision,
        payload={field: entity_id, "removed_at": recorded_at},
        recorded_at=recorded_at,
    )


def catalog_invalidated_event(
    entity_id: str,
    *,
    revision: int,
    recorded_at: float,
    reason: str,
) -> JournalEventRecord:
    return JournalEventRecord(
        kind="catalog.invalidated",
        entity_id=entity_id,
        entity_revision=revision,
        payload={"reason": reason},
        recorded_at=recorded_at,
    )


__all__ = [
    "catalog_invalidated_event",
    "control_event",
    "job_event",
    "next_revision",
    "participant_event",
    "terminal_binding_event",
    "tombstone_event",
    "workspace_event",
    "workspace_usage_event",
]
