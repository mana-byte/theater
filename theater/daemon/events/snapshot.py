"""Immutable, bounded public state snapshots over one SQLite read transaction."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import Connection, func, select

from theater.daemon.events import projection as event_projection
from theater.daemon.events.reader import JournalReader, StateReadError, StreamCursor
from theater.daemon.operations import UNSETTLED_STATES, operation_to_wire
from theater.daemon.schema import (
    jobs,
    participants,
    providers,
    public_operations,
    usage,
    workspaces,
)
from theater.frontend.capabilities import MAX_FRAME_BYTES, PUBLIC_LIMITS
from theater.models import Job, Participant, Status, new_id, now

CachedParticipantProjection = event_projection.CachedParticipantProjection
ParticipantProjectionFacts = event_projection.ParticipantProjectionFacts
ParticipantProjectionResolver = event_projection.ParticipantProjectionResolver
configure_participant_projection = event_projection.configure_participant_projection
_configured_participant_projection = event_projection._configured_participant_projection
_participant_projection_facts = event_projection._participant_projection_facts
_project_terminal_route = event_projection._project_terminal_route
_native_route_is_valid = event_projection._native_route_is_valid
_route_flag = event_projection._route_flag
_route_available = event_projection._route_available
_participant_projection = event_projection._participant_projection
_job_projection = event_projection._job_projection
_provider_projection = event_projection._provider_projection
_workspace_projection = event_projection._workspace_projection
_usage_to_wire = event_projection._usage_to_wire
_token_revision = event_projection._token_revision

_PAGE_RESPONSE_BYTES = MAX_FRAME_BYTES - 4096
_PAGE_DEFAULT = int(PUBLIC_LIMITS["entity_page_default"])
_PAGE_MAX = int(PUBLIC_LIMITS["entity_page_max"])


@dataclass(frozen=True, slots=True)
class _Snapshot:
    actor_client_id: str
    expires_at: float
    pages: tuple[bytes, ...]
    byte_size: int


class SnapshotCache:
    """Own immutable encoded pages until their actor releases or outlives them."""

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        lifetime_seconds: float,
        snapshots_per_client: int,
        max_bytes: int,
    ) -> None:
        if lifetime_seconds <= 0 or snapshots_per_client < 1 or max_bytes < 1:
            raise ValueError("snapshot cache limits must be positive")
        self._clock = clock
        self._lifetime_seconds = lifetime_seconds
        self._snapshots_per_client = snapshots_per_client
        self._max_bytes = max_bytes
        self._snapshots: dict[str, _Snapshot] = {}
        self._actor_ids: dict[str, set[str]] = {}
        self._used_bytes = 0

    @property
    def used_bytes(self) -> int:
        return self._used_bytes

    def put(self, snapshot_id: str, actor_client_id: str, pages: tuple[bytes, ...]) -> None:
        self.expire()
        if snapshot_id in self._snapshots:
            raise StateReadError("internal", "snapshot identifier collision")
        actor_ids = self._actor_ids.get(actor_client_id, set())
        if len(actor_ids) >= self._snapshots_per_client:
            raise StateReadError(
                "too_large",
                "this client already holds the maximum number of state snapshots",
                {"snapshots_per_client": self._snapshots_per_client},
            )
        byte_size = sum(len(page) for page in pages)
        if byte_size > self._max_bytes or self._used_bytes + byte_size > self._max_bytes:
            raise StateReadError(
                "too_large",
                "the active state projection cannot fit in the snapshot cache",
                {"snapshot_cache_bytes": self._max_bytes},
            )
        self._snapshots[snapshot_id] = _Snapshot(
            actor_client_id=actor_client_id,
            expires_at=self._clock() + self._lifetime_seconds,
            pages=pages,
            byte_size=byte_size,
        )
        self._actor_ids.setdefault(actor_client_id, set()).add(snapshot_id)
        self._used_bytes += byte_size

    def page(self, snapshot_id: str, actor_client_id: str, page: int) -> dict[str, object]:
        if type(page) is not int or page < 0:
            raise StateReadError("bad_request", "snapshot page must be a non-negative integer")
        snapshot = self._owned(snapshot_id, actor_client_id)
        if page >= len(snapshot.pages):
            raise StateReadError(
                "bad_request",
                "the requested snapshot page does not exist",
                {"snapshot_id": snapshot_id, "page": page},
            )
        value = json.loads(snapshot.pages[page])
        if not isinstance(value, dict):
            raise StateReadError("internal", "cached snapshot page is invalid")
        return value

    def release(self, snapshot_id: str, actor_client_id: str) -> None:
        self._owned(snapshot_id, actor_client_id)
        self._remove(snapshot_id)

    def expire(self) -> None:
        timestamp = self._clock()
        for snapshot_id, snapshot in tuple(self._snapshots.items()):
            if snapshot.expires_at <= timestamp:
                self._remove(snapshot_id)

    def _owned(self, snapshot_id: str, actor_client_id: str) -> _Snapshot:
        self.expire()
        snapshot = self._snapshots.get(snapshot_id)
        if snapshot is None or snapshot.actor_client_id != actor_client_id:
            raise StateReadError(
                "snapshot_expired",
                "the state snapshot is missing, expired, or belongs to another client",
                {"snapshot_id": snapshot_id},
            )
        return snapshot

    def _remove(self, snapshot_id: str) -> None:
        snapshot = self._snapshots.pop(snapshot_id, None)
        if snapshot is None:
            return
        self._used_bytes -= snapshot.byte_size
        actor_ids = self._actor_ids.get(snapshot.actor_client_id)
        if actor_ids is not None:
            actor_ids.discard(snapshot_id)
            if not actor_ids:
                self._actor_ids.pop(snapshot.actor_client_id, None)


class SnapshotService:
    """Materialize committed active state, then serve it from immutable pages."""

    def __init__(
        self,
        store,
        *,
        participant_name: Callable[[str], str | None] | None = None,
        provider_health: Callable[[str], str] | None = None,
        participant_projection: ParticipantProjectionResolver | None = None,
        clock: Callable[[], float] = now,
        id_factory: Callable[[], str] = new_id,
        lifetime_seconds: float = float(PUBLIC_LIMITS["snapshot_lifetime_seconds"]),
        snapshots_per_client: int = int(PUBLIC_LIMITS["snapshots_per_client"]),
        cache_bytes: int = int(PUBLIC_LIMITS["snapshot_cache_bytes"]),
    ) -> None:
        self._store = store
        self._reader = JournalReader(store.journal)
        self._id_factory = id_factory
        fallback_name = getattr(store, "participant_projection_name", None)
        self._participant_name = participant_name or fallback_name or (lambda _participant_id: None)
        self._provider_health = provider_health or (lambda _provider_id: "unknown")
        self._participant_projection = participant_projection or _configured_participant_projection(
            store
        )
        self.cache = SnapshotCache(
            clock=clock,
            lifetime_seconds=lifetime_seconds,
            snapshots_per_client=snapshots_per_client,
            max_bytes=cache_bytes,
        )

    def snapshot(
        self, actor_client_id: str, *, page_size: int = _PAGE_DEFAULT
    ) -> dict[str, object]:
        if type(page_size) is not int or not 1 <= page_size <= _PAGE_MAX:
            raise StateReadError("bad_request", "snapshot page size must be between 1 and 500")
        projection, cursor = self._materialize()
        snapshot_id = self._id_factory()
        pages = _encode_pages(snapshot_id, projection, cursor, page_size)
        self.cache.put(snapshot_id, actor_client_id, pages)
        return self.cache.page(snapshot_id, actor_client_id, 0)

    def page(self, actor_client_id: str, snapshot_id: str, page: int) -> dict[str, object]:
        return self.cache.page(snapshot_id, actor_client_id, page)

    def release(self, actor_client_id: str, snapshot_id: str) -> None:
        self.cache.release(snapshot_id, actor_client_id)

    def _materialize(self) -> tuple[dict[str, object], StreamCursor]:
        with self._store.engine.connect() as connection:
            # sqlite3 legacy transaction mode does not begin on SELECT; make the
            # promised multi-query snapshot explicit before the first read.
            connection.exec_driver_sql("BEGIN")
            active_participants = [
                Participant.from_row(row._mapping)
                for row in connection.execute(
                    select(participants)
                    .where(participants.c.status != Status.DEAD.value)
                    .order_by(participants.c.created_at.asc(), participants.c.id.asc())
                )
            ]
            participant_values = [
                _participant_projection(
                    self._store,
                    participant,
                    connection,
                    name=self._participant_name(participant.id),
                    projection=self._participant_projection,
                    transactional=True,
                )
                for participant in active_participants
            ]
            operation_values = [
                operation_to_wire(record)
                for operation_id in connection.execute(
                    select(public_operations.c.operation_id)
                    .where(public_operations.c.state.in_(UNSETTLED_STATES))
                    .order_by(
                        public_operations.c.created_at.asc(),
                        public_operations.c.operation_id.asc(),
                    )
                ).scalars()
                if (record := self._store.operations.get(str(operation_id), connection=connection))
                is not None
            ]
            job_values = [
                _job_projection(Job.from_row(row._mapping))
                for row in connection.execute(
                    select(jobs)
                    .where(jobs.c.state == "running")
                    .order_by(jobs.c.created_at.asc(), jobs.c.handle.asc())
                )
            ]
            provider_values = [
                _provider_projection(
                    record,
                    health=self._provider_health(record.provider_id),
                )
                for provider_id in connection.execute(
                    select(providers.c.provider_id).order_by(
                        providers.c.selector.asc(), providers.c.provider_id.asc()
                    )
                ).scalars()
                if (record := self._store.providers.get(str(provider_id), connection=connection))
                is not None
            ]
            workspace_values = [
                _workspace_projection(self._store, record, connection)
                for workspace_id in connection.execute(
                    select(workspaces.c.workspace_id)
                    .where(workspaces.c.state != "removed")
                    .order_by(workspaces.c.created_at.asc(), workspaces.c.workspace_id.asc())
                ).scalars()
                if (record := self._store.workspaces.get(str(workspace_id), connection=connection))
                is not None
            ]
            usage_values = _usage_projection(connection)
            cursor = self._reader.cursor(connection=connection)
        return (
            {
                "participants": participant_values,
                "operations": operation_values,
                "jobs": job_values,
                "providers": provider_values,
                "workspaces": workspace_values,
                "usage": usage_values,
            },
            cursor,
        )


def _usage_projection(connection: Connection) -> dict[str, int]:
    row = connection.execute(
        select(
            func.coalesce(func.sum(usage.c.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(usage.c.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(usage.c.cache_creation_input_tokens), 0).label(
                "cache_creation_input_tokens"
            ),
            func.coalesce(func.sum(usage.c.cache_read_input_tokens), 0).label(
                "cache_read_input_tokens"
            ),
            func.coalesce(func.sum(usage.c.reasoning_output_tokens), 0).label(
                "reasoning_output_tokens"
            ),
            func.coalesce(func.sum(usage.c.cost_microcents), 0).label("cost_microcents"),
        )
    ).one()
    return {key: int(value) for key, value in row._mapping.items()}


def _encode_pages(
    snapshot_id: str,
    projection: dict[str, object],
    cursor: StreamCursor,
    page_size: int,
) -> tuple[bytes, ...]:
    collections: dict[str, list[object]] = {}
    for name in ("participants", "operations", "jobs", "providers", "workspaces"):
        values = projection[name]
        if not isinstance(values, list):
            raise StateReadError("internal", "state snapshot projection is invalid")
        collections[name] = values
    page_count = max(
        1,
        *((len(values) + page_size - 1) // page_size for values in collections.values()),
    )
    pages: list[bytes] = []
    for page in range(page_count):
        result: dict[str, object] = {
            "snapshot_id": snapshot_id,
            "page": page,
            "complete": page == page_count - 1,
            "ending_cursor": cursor.to_wire(),
        }
        for name, values in collections.items():
            result[name] = values[page * page_size : (page + 1) * page_size]
        if page == 0:
            result["usage"] = projection["usage"]
        encoded = _encode_page(result)
        if len(encoded) > _PAGE_RESPONSE_BYTES:
            raise StateReadError(
                "too_large",
                "one immutable state snapshot page exceeds the public frame limit",
                {"limit_bytes": _PAGE_RESPONSE_BYTES},
            )
        pages.append(encoded)
    return tuple(pages)


def _encode_page(value: dict[str, object]) -> bytes:
    try:
        return json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise StateReadError("internal", "state snapshot contains invalid persisted data") from exc


__all__ = [
    "CachedParticipantProjection",
    "ParticipantProjectionFacts",
    "SnapshotCache",
    "SnapshotService",
    "configure_participant_projection",
]
