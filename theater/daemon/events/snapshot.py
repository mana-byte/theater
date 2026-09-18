"""Immutable, bounded public state snapshots over one SQLite read transaction."""

from __future__ import annotations

import json
import math
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sqlalchemy import Connection, func, select

from theater.daemon.events.reader import JournalReader, StateReadError, StreamCursor
from theater.daemon.operations import UNSETTLED_STATES, operation_to_wire
from theater.daemon.schema import (
    jobs,
    participant_runtime_bindings,
    participants,
    providers,
    public_operations,
    usage,
    workspaces,
)
from theater.frontend.capabilities import MAX_FRAME_BYTES, PUBLIC_LIMITS
from theater.harness.contracts.runtime import ConnectionHealth, RuntimeCapability, RuntimeWiring
from theater.models import (
    ControlOwnerKind,
    Job,
    Participant,
    ProviderRecord,
    Status,
    TerminalBindingRecord,
    WorkspaceRecord,
    WorkspaceUsageRecord,
    new_id,
    now,
)
from theater.provenance import is_trusted_provenance

_PAGE_RESPONSE_BYTES = MAX_FRAME_BYTES - 4096
_PAGE_DEFAULT = int(PUBLIC_LIMITS["entity_page_default"])
_PAGE_MAX = int(PUBLIC_LIMITS["entity_page_max"])


@dataclass(frozen=True, slots=True)
class _Snapshot:
    actor_client_id: str
    expires_at: float
    pages: tuple[bytes, ...]
    byte_size: int


@dataclass(frozen=True, slots=True)
class ParticipantProjectionFacts:
    """Cached, non-I/O facts that can safely accompany one durable row."""

    presence: str
    terminal_route: Mapping[str, object] | None
    native_route: Mapping[str, object] | None
    actions: Mapping[str, Mapping[str, object]]
    addressable: bool


type ParticipantProjectionResolver = Callable[
    [
        Participant,
        TerminalBindingRecord | None,
        Mapping[str, object] | None,
        bool,
        Connection | None,
    ],
    ParticipantProjectionFacts,
]


_participant_projection_resolvers: weakref.WeakKeyDictionary[
    object, weakref.ReferenceType[ParticipantProjectionResolver]
] = weakref.WeakKeyDictionary()


def configure_participant_projection(
    store: object, resolver: ParticipantProjectionResolver
) -> None:
    """Install daemon-composed cached facts for snapshot and event projections."""
    _participant_projection_resolvers[store] = weakref.ref(resolver)


def _configured_participant_projection(store: object) -> ParticipantProjectionResolver | None:
    try:
        reference = _participant_projection_resolvers.get(store)
    except TypeError:
        return None
    return None if reference is None else reference()


class CachedParticipantProjection:
    """Project current cached route and presence facts without runtime I/O."""

    def __init__(
        self,
        *,
        presence_snapshot: Callable[[str], object],
        terminal_projection: Callable[[TerminalBindingRecord], Mapping[str, object]],
        route_for: Callable[[str, RuntimeCapability], object],
        provider_health: Callable[[str, int], str],
        native_route: Callable[
            [Participant, Mapping[str, object] | None], Mapping[str, object] | None
        ]
        | None = None,
        transactional_route_for: Callable[[str, RuntimeCapability, Connection], object]
        | None = None,
    ) -> None:
        self._presence_snapshot = presence_snapshot
        self._terminal_projection = terminal_projection
        self._route_for = route_for
        self._provider_health = provider_health
        self._native_route = native_route
        self._transactional_route_for = transactional_route_for

    def __call__(
        self,
        participant: Participant,
        binding: TerminalBindingRecord | None,
        durable_native_route: Mapping[str, object] | None,
        transactional: bool,
        connection: Connection | None = None,
    ) -> ParticipantProjectionFacts:
        terminal_route = _project_terminal_route(
            binding,
            self._terminal_projection,
            preserve_pending_health=transactional,
        )
        native_route = self._project_native_route(participant, durable_native_route)
        presence = self._presence(participant.id)
        actions = self._actions(
            participant,
            terminal_route,
            native_route,
            presence,
            connection=connection if transactional else None,
        )
        addressable = participant.status is not Status.DEAD and any(
            action["route_available"] is True for action in actions.values()
        )
        return ParticipantProjectionFacts(
            presence=presence,
            terminal_route=terminal_route,
            native_route=native_route,
            actions=actions,
            addressable=addressable,
        )

    def _presence(self, participant_id: str) -> str:
        try:
            snapshot = self._presence_snapshot(participant_id)
            state = getattr(snapshot, "state", None)
            value = getattr(state, "value", state)
            if getattr(snapshot, "reason", None) == "unregistered":
                return "unknown"
            return value if isinstance(value, str) and value else "unknown"
        except Exception:
            return "unknown"

    def _project_native_route(
        self,
        participant: Participant,
        durable_native_route: Mapping[str, object] | None,
    ) -> Mapping[str, object] | None:
        if self._native_route is not None:
            try:
                current = self._native_route(participant, durable_native_route)
            except Exception:
                current = None
            if _native_route_is_valid(current):
                assert isinstance(current, Mapping)
                return dict(current)
        return None if durable_native_route is None else dict(durable_native_route)

    def _actions(
        self,
        participant: Participant,
        terminal_route: Mapping[str, object] | None,
        native_route: Mapping[str, object] | None,
        presence: str,
        *,
        connection: Connection | None,
    ) -> dict[str, dict[str, object]]:
        actions: dict[str, dict[str, object]] = {}
        for capability in RuntimeCapability:
            try:
                route = (
                    self._transactional_route_for(participant.id, capability, connection)
                    if connection is not None and self._transactional_route_for is not None
                    else self._route_for(participant.id, capability)
                )
            except Exception:
                route = None
            supported = getattr(route, "transport", None) is not None
            route_available = _route_available(
                route,
                terminal_route,
                native_route,
                provider_health=self._provider_health,
            )
            admissible = (
                participant.status is not Status.DEAD
                and supported
                and route_available
                and presence == "absent"
            )
            reason, detail = _action_reason(
                participant,
                route,
                supported=supported,
                route_available=route_available,
                presence=presence,
            )
            actions[capability.value] = {
                "supported": supported,
                "route_available": route_available,
                "admissible": admissible,
                "reason": reason,
                "detail": detail,
            }
        return actions


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
        with self._store.engine.connect() as connection, connection.begin():
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


def _participant_projection_facts(
    store: object,
    participant: Participant,
    binding: TerminalBindingRecord | None,
    native_route: Mapping[str, object] | None,
    connection: Connection,
    *,
    projection: ParticipantProjectionResolver | None,
    transactional: bool,
) -> ParticipantProjectionFacts | None:
    resolver = projection or _configured_participant_projection(store)
    if resolver is None:
        return None
    try:
        value = resolver(participant, binding, native_route, transactional, connection)
    except Exception:
        return None
    return value if isinstance(value, ParticipantProjectionFacts) else None


def _project_terminal_route(
    binding: TerminalBindingRecord | None,
    projection: Callable[[TerminalBindingRecord], Mapping[str, object]] | None = None,
    *,
    preserve_pending_health: bool = False,
) -> dict[str, object] | None:
    if binding is None:
        return None
    health = binding.health
    report_revision = binding.report_revision
    if projection is not None:
        try:
            current = projection(binding)
        except Exception:
            current = None
        if isinstance(current, Mapping):
            value = current.get("health")
            if (
                isinstance(value, str)
                and value
                and not (
                    preserve_pending_health
                    and binding.health == "reconciling"
                    and value == "offline"
                )
            ):
                health = value
            revision = current.get("report_revision")
            if type(revision) is int and revision >= 0:
                report_revision = revision
    return {
        "identity": {
            "provider_id": binding.provider_id,
            "provider_generation": binding.provider_generation,
            "terminal_id": binding.terminal_id,
            "terminal_incarnation": binding.terminal_incarnation,
            "occupant": dict(binding.occupant_evidence),
            "process": None if binding.process_facts is None else dict(binding.process_facts),
        },
        "health": health,
        "report_revision": report_revision,
    }


def _native_route_is_valid(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    return (
        type(value.get("backend_generation")) is int
        and isinstance(value.get("native_session_id"), str)
        and isinstance(value.get("health"), str)
    )


def _route_flag(route: object, name: str) -> bool:
    try:
        return bool(getattr(route, name, False))
    except Exception:
        return False


def _route_available(
    route: object,
    terminal_route: Mapping[str, object] | None,
    native_route: Mapping[str, object] | None,
    *,
    provider_health: Callable[[str, int], str],
) -> bool:
    if _route_flag(route, "is_provider"):
        if terminal_route is None:
            return False
        identity = terminal_route.get("identity")
        if not isinstance(identity, Mapping) or terminal_route.get("health") != "healthy":
            return False
        provider_id = identity.get("provider_id")
        generation = identity.get("provider_generation")
        if not isinstance(provider_id, str) or type(generation) is not int:
            return False
        try:
            health = provider_health(provider_id, generation)
        except Exception:
            return False
        # A completed inventory is committed immediately before the callback
        # peer flips reconciling -> online, so this one cached transition is
        # already the route that becomes usable with the transaction.
        return health in {"online", "reconciling"}
    if _route_flag(route, "is_native"):
        return native_route is not None and native_route.get("health") in {
            ConnectionHealth.CONNECTED.value,
            ConnectionHealth.DEGRADED.value,
        }
    # Legacy pane fields are migration data, not a current provider delivery route.
    return False


def _action_reason(
    participant: Participant,
    route: object,
    *,
    supported: bool,
    route_available: bool,
    presence: str,
) -> tuple[str | None, str | None]:
    if participant.status is Status.DEAD:
        return "not_addressable", "the participant is dead"
    if not supported:
        unavailable = getattr(route, "unavailable_reason", None)
        value = getattr(unavailable, "value", unavailable)
        return (str(value) if isinstance(value, str) and value else "unsupported"), None
    if not route_available:
        return "route_unavailable", None
    if presence != "absent":
        return ("human_present" if presence == "present" else "presence_unknown"), None
    return None, None


def _participant_projection(
    store,
    participant: Participant,
    connection: Connection,
    *,
    name: str | None = None,
    projection: ParticipantProjectionResolver | None = None,
    transactional: bool = False,
) -> dict[str, object]:
    binding = store.terminal_bindings.get(participant.id, connection=connection)
    terminal_route = _project_terminal_route(binding, preserve_pending_health=transactional)
    owner_kind = participant.control_owner_kind or ControlOwnerKind.LOCAL_OPERATOR
    owner = {
        "kind": owner_kind.value,
        "participant_id": (
            participant.control_owner_id if owner_kind is ControlOwnerKind.PARTICIPANT else None
        ),
        "revision": participant.control_revision,
    }
    runtime_binding = connection.execute(
        select(
            participant_runtime_bindings.c.backend_generation,
            participant_runtime_bindings.c.native_session_id,
        )
        .where(participant_runtime_bindings.c.participant_id == participant.id)
        .where(participant_runtime_bindings.c.wiring == RuntimeWiring.NATIVE.value)
    ).first()
    native_route: dict[str, object] | None = None
    if runtime_binding is not None:
        native_route = {
            "backend_generation": int(runtime_binding.backend_generation),
            "native_session_id": runtime_binding.native_session_id,
            # A durable binding is restart evidence, not a live route.
            "health": ConnectionHealth.DISCONNECTED.value,
        }
    facts = _participant_projection_facts(
        store,
        participant,
        binding,
        native_route,
        connection,
        projection=projection,
        transactional=transactional,
    )
    if facts is not None:
        terminal_route = None if facts.terminal_route is None else dict(facts.terminal_route)
        native_route = None if facts.native_route is None else dict(facts.native_route)
    trusted_identity: dict[str, object] | None = None
    if participant.session_id is not None and is_trusted_provenance(
        participant.session_correlation
    ):
        trusted_identity = {
            "session_id": participant.session_id,
            "provenance": participant.session_correlation,
        }
    return {
        "participant_id": participant.id,
        "origin": (
            participant.origin.value if participant.origin is not None else participant.tier.value
        ),
        "harness": participant.harness,
        "status": participant.status.value,
        "owner": owner,
        "parent_id": participant.parent_id,
        "cwd": participant.cwd,
        "workspace_id": participant.workspace_id,
        "name": name,
        "description": participant.description,
        "addressable": facts.addressable if facts is not None else False,
        "presence": facts.presence if facts is not None else "unknown",
        "terminal_route": terminal_route,
        "native_route": native_route,
        "trusted_identity": trusted_identity,
        "actions": (
            {} if facts is None else {key: dict(value) for key, value in facts.actions.items()}
        ),
        "projection_revision": participant.control_revision,
    }


def _job_projection(job: Job) -> dict[str, object]:
    actor: dict[str, object] | None = None
    legacy_caller_id: str | None = None
    if job.actor_client_id:
        actor = {"client_id": job.actor_client_id, "participant_id": job.actor_participant_id}
    elif job.caller_id:
        legacy_caller_id = job.caller_id
    else:
        raise StateReadError("internal", f"stored job {job.handle!r} has no actor identity")
    error = None
    if job.error_code:
        error = {
            "code": job.error_code,
            "message": f"job finished with error code {job.error_code!r}",
        }
    return {
        "handle": job.handle,
        "state": str(job.state),
        "kind": str(job.kind),
        "actor": actor,
        "legacy_caller_id": legacy_caller_id,
        "target_id": job.target_id,
        "result": _job_result(job),
        "error": error,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "structured_status": job.structured_status,
        "raw_result": job.result,
        "error_code": job.error_code,
        "response_format": job.response_format,
    }


def _job_result(job: Job) -> object:
    if job.structured_status != "parsed" or job.structured_result is None:
        return job.result
    try:
        value = json.loads(job.structured_result, parse_constant=_reject_nonfinite)
    except (TypeError, ValueError, RecursionError):
        return job.result
    return value if _finite_json(value) else job.result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r}")


def _finite_json(value: object) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite_json(item) for item in value.values())
    if isinstance(value, list):
        return all(_finite_json(item) for item in value)
    return True


def _provider_projection(record: ProviderRecord, *, health: str = "unknown") -> dict[str, object]:
    return {
        "provider_id": record.provider_id,
        "selector": record.selector,
        "kind": record.kind,
        "generation": record.generation,
        "health": health,
        "capabilities": list(record.capabilities),
        "limits": dict(record.limits),
        "last_report_revision": record.last_report_revision,
        "configuration_version": record.configuration_version,
    }


def _workspace_projection(
    store, record: WorkspaceRecord, connection: Connection
) -> dict[str, object]:
    usages = store.workspaces.active_usages(record.workspace_id, connection=connection)
    fence = None
    if record.deletion_token is not None:
        fence = {
            "token": record.deletion_token,
            "revision": _token_revision(record.deletion_token),
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
        "usages": [_usage_to_wire(item) for item in usages],
        "deletion_fence": fence,
    }


def _usage_to_wire(record: WorkspaceUsageRecord) -> dict[str, object]:
    return {
        "usage_id": record.usage_id,
        "workspace_id": record.workspace_id,
        "holder_kind": record.holder_kind,
        "holder_id": record.holder_id,
        "acquired_at": record.acquired_at,
        "released_at": record.released_at,
        "release_reason": record.release_reason,
    }


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


def _token_revision(token: str) -> int:
    try:
        return max(0, int(token.split("-", 1)[0]))
    except ValueError:
        return 0


__all__ = [
    "CachedParticipantProjection",
    "ParticipantProjectionFacts",
    "SnapshotCache",
    "SnapshotService",
    "configure_participant_projection",
]
