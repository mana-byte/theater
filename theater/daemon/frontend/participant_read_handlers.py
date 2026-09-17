"""Read-only public participant projections backed by daemon facts."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from sqlalchemy import and_, or_, select

from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.frontend.validation import PublicRequestError
from theater.daemon.presence import access as presence_access
from theater.daemon.rpc.controls import _effective_capabilities
from theater.daemon.schema import participants
from theater.frontend.capabilities import METHOD_CATALOG
from theater.frontend.schemas import validator_for
from theater.harness.contracts.runtime import ConnectionHealth, RuntimeCapability, RuntimeSnapshot
from theater.models import ControlOwnerKind, NotFound, Participant, Status
from theater.provenance import is_trusted_provenance

_PAGE_DEFAULT = 200
_PAGE_MAX = 500


def _validated(method: str, result: dict[str, object]) -> dict[str, object]:
    validator_for(METHOD_CATALOG[method].result_schema_id).validate(result)
    return result


async def _runtime_snapshot(daemon, participant_id: str) -> RuntimeSnapshot | None:
    manager = getattr(daemon, "runtime_manager", None)
    runtime_for = getattr(manager, "get", None)
    runtime = runtime_for(participant_id) if callable(runtime_for) else None
    if runtime is None:
        return None
    try:
        snapshot = await runtime.snapshot()
    except Exception:
        return None
    return snapshot if isinstance(snapshot, RuntimeSnapshot) else None


def _terminal_route(daemon, participant) -> dict[str, object] | None:
    binding = daemon.store.terminal_bindings.get(participant.id)
    if binding is None:
        return None
    terminal_service = getattr(daemon, "terminal_service", None)
    projection = (
        terminal_service.binding_projection(binding)
        if terminal_service is not None
        else {
            "provider_id": binding.provider_id,
            "provider_generation": binding.provider_generation,
            "terminal_id": binding.terminal_id,
            "terminal_incarnation": binding.terminal_incarnation,
            "occupant": dict(binding.occupant_evidence),
            "process": None if binding.process_facts is None else dict(binding.process_facts),
            "health": binding.health,
        }
    )
    identity = {
        key: projection[key]
        for key in (
            "provider_id",
            "provider_generation",
            "terminal_id",
            "terminal_incarnation",
            "occupant",
            "process",
        )
    }
    return {"identity": identity, "health": projection["health"]}


def _native_route(
    daemon, participant, snapshot: RuntimeSnapshot | None
) -> dict[str, object] | None:
    binding = daemon.store.get_runtime_binding(participant.id)
    if snapshot is not None:
        return {
            "backend_generation": snapshot.backend_generation,
            "native_session_id": snapshot.native_session_id,
            "health": str(snapshot.health),
        }
    if binding is None:
        return None
    return {
        "backend_generation": binding.backend_generation,
        "native_session_id": binding.native_session_id,
        "health": ConnectionHealth.DISCONNECTED.value,
    }


def _owner(participant) -> dict[str, object]:
    kind = participant.control_owner_kind or ControlOwnerKind.LOCAL_OPERATOR
    owner: dict[str, object] = {
        "kind": str(kind),
        "participant_id": None,
        "revision": participant.control_revision,
    }
    if kind is ControlOwnerKind.PARTICIPANT:
        owner["participant_id"] = participant.control_owner_id
    return owner


def _route_flag(route, name: str) -> bool:
    """Read an additive route flag without making an older route unsafe."""
    try:
        return bool(getattr(route, name, False))
    except Exception:
        return False


def _provider_route_available(route, terminal_route: Mapping[str, object] | None) -> bool:
    """Require the route's current provider generation and its projected terminal."""
    if not _route_flag(route, "route_available") or terminal_route is None:
        return False
    terminal = getattr(route, "terminal", None)
    provider_id = getattr(terminal, "provider_id", None)
    generation = getattr(terminal, "provider_generation", None)
    identity = terminal_route.get("identity")
    if not isinstance(identity, Mapping):
        return False
    return (
        isinstance(provider_id, str)
        and type(generation) is int
        and identity.get("provider_id") == provider_id
        and identity.get("provider_generation") == generation
        and terminal_route.get("health") == "healthy"
    )


def _legacy_pane_available(participant) -> bool:
    """The compatibility route is a verified live pane, never a provider binding."""
    return (
        participant.status is not Status.DEAD
        and participant.tmux_pane is not None
        and participant.addressable
    )


def _physical_route_available(
    route,
    participant,
    terminal_route: Mapping[str, object] | None,
    native_route: Mapping[str, object] | None,
) -> bool:
    if _route_flag(route, "is_provider"):
        return _provider_route_available(route, terminal_route)
    if _route_flag(route, "is_native"):
        return native_route is not None and native_route.get("health") in {
            ConnectionHealth.CONNECTED.value,
            ConnectionHealth.DEGRADED.value,
        }
    if _route_flag(route, "is_legacy"):
        return _legacy_pane_available(participant)
    return False


def _actions(
    daemon,
    participant,
    *,
    snapshot: RuntimeSnapshot | None,
    terminal_route: Mapping[str, object] | None,
    native_route: Mapping[str, object] | None,
    presence,
) -> dict[str, dict[str, object]]:
    capabilities = _effective_capabilities(daemon, participant, snapshot)
    actions: dict[str, dict[str, object]] = {}
    for capability in RuntimeCapability:
        route = daemon.controls.route_for(participant.id, capability)
        report = capabilities[capability.value]
        reported_available = report.get("available") is True
        supported = route.transport is not None
        if (_route_flag(route, "is_native") and snapshot is not None) or _route_flag(
            route, "is_legacy"
        ):
            supported = reported_available
        route_available = _physical_route_available(
            route, participant, terminal_route, native_route
        )
        admissible = (
            participant.status is not Status.DEAD
            and supported
            and route_available
            and not presence.protected
        )
        reason: str | None = None
        detail: str | None = None
        if participant.status is Status.DEAD:
            reason = "not_addressable"
            detail = "the participant is dead"
        elif not supported:
            value = report.get("reason")
            reason = str(value) if value is not None else "unsupported"
            raw_detail = report.get("detail")
            detail = str(raw_detail) if raw_detail is not None else None
        elif not route_available:
            reason = "route_unavailable"
            raw_detail = report.get("detail")
            detail = str(raw_detail) if raw_detail is not None else None
        elif presence.protected:
            reason = "human_present" if presence.state.value == "present" else "presence_unknown"
            detail = presence.reason
        actions[capability.value] = {
            "supported": supported,
            "route_available": route_available,
            "admissible": admissible,
            "reason": reason,
            "detail": detail,
        }
    return actions


async def participant_to_wire(daemon, participant) -> dict[str, object]:
    """Project current route facts without using legacy tier as a policy proxy."""
    snapshot = await _runtime_snapshot(daemon, participant.id)
    terminal_route = _terminal_route(daemon, participant)
    native_route = _native_route(daemon, participant, snapshot)
    presence = presence_access.presence_snapshot(daemon, participant.id)
    actions = _actions(
        daemon,
        participant,
        snapshot=snapshot,
        terminal_route=terminal_route,
        native_route=native_route,
        presence=presence,
    )
    trusted_identity: dict[str, object] | None = None
    if participant.session_id is not None and is_trusted_provenance(
        participant.session_correlation
    ):
        trusted_identity = {
            "session_id": participant.session_id,
            "provenance": participant.session_correlation,
        }
    origin = participant.origin.value if participant.origin is not None else participant.tier.value
    active_native = native_route is not None and native_route["health"] in {
        ConnectionHealth.CONNECTED.value,
        ConnectionHealth.DEGRADED.value,
    }
    return {
        "participant_id": participant.id,
        "origin": origin,
        "harness": participant.harness,
        "status": participant.status.value,
        "owner": _owner(participant),
        "parent_id": participant.parent_id,
        "cwd": participant.cwd,
        "workspace_id": participant.workspace_id,
        "name": participant.name,
        "description": participant.description,
        "addressable": participant.status is not Status.DEAD
        and (
            _legacy_pane_available(participant)
            or active_native
            or any(action["route_available"] for action in actions.values())
        ),
        "presence": presence.state.value,
        "terminal_route": terminal_route,
        "native_route": native_route,
        "trusted_identity": trusted_identity,
        "actions": actions,
    }


def _participant_page(daemon, params: dict) -> tuple[list[Participant], str | None]:
    limit = params.get("limit", _PAGE_DEFAULT)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _PAGE_MAX:
        raise PublicRequestError("bad_request", "participant page limit must be between 1 and 500")
    statement = select(participants)
    status = params.get("status")
    if status is not None:
        statement = statement.where(participants.c.status == status)
    owner_id = params.get("owner_id")
    if owner_id is not None:
        statement = statement.where(participants.c.control_owner_id == owner_id)
    cursor = params.get("cursor")
    if cursor is not None:
        marker = daemon.store.get_participant(cursor)
        if marker is None:
            raise PublicRequestError("bad_request", f"unknown participant cursor {cursor!r}")
        statement = statement.where(
            or_(
                participants.c.created_at > marker.created_at,
                and_(
                    participants.c.created_at == marker.created_at,
                    participants.c.id > marker.id,
                ),
            )
        )
    ordered = statement.order_by(participants.c.created_at.asc(), participants.c.id.asc())
    rows = daemon.store.conn.execute(ordered.limit(limit + 1)).fetchall()
    participants_page = [Participant.from_row(row._mapping) for row in rows]
    has_more = len(participants_page) > limit
    page = [daemon.registry.get(row.id) for row in participants_page[:limit]]
    return page, page[-1].id if has_more and page else None


async def participants_list(daemon, _context: ConnectionContext, params: dict) -> dict[str, object]:
    rows, next_cursor = _participant_page(daemon, params)
    return _validated(
        "frontend.participants.list",
        {
            "items": [await participant_to_wire(daemon, row) for row in rows],
            "next_cursor": next_cursor,
        },
    )


async def participants_get(daemon, _context: ConnectionContext, params: dict) -> dict[str, object]:
    participant_id = params["participant_id"]
    try:
        participant = daemon.registry.get(participant_id)
    except NotFound as exc:
        raise PublicRequestError(
            "not_found", f"no participant {participant_id!r}", {"participant_id": participant_id}
        ) from exc
    return _validated("frontend.participants.get", await participant_to_wire(daemon, participant))


async def participants_tree(daemon, _context: ConnectionContext, params: dict) -> dict[str, object]:
    participant_id = params["participant_id"]
    try:
        root = daemon.registry.get(participant_id)
    except NotFound as exc:
        raise PublicRequestError(
            "not_found", f"no participant {participant_id!r}", {"participant_id": participant_id}
        ) from exc
    rows = daemon.registry.list(include_dead=True)
    children: dict[str, list] = {}
    for row in rows:
        if row.parent_id is not None:
            children.setdefault(row.parent_id, []).append(row)
    descendants: list[Participant] = []
    seen: set[str] = set()
    pending = [root]
    while pending and len(descendants) < _PAGE_MAX + 1:
        current = pending.pop(0)
        if current.id in seen:
            continue
        seen.add(current.id)
        descendants.append(current)
        pending.extend(children.get(current.id, ()))
    if len(descendants) > _PAGE_MAX or pending:
        raise PublicRequestError(
            "too_large",
            "participant lineage exceeds the public tree limit",
            {"participant_id": root.id, "limit": _PAGE_MAX},
        )
    return _validated(
        "frontend.participants.tree",
        {
            "items": [await participant_to_wire(daemon, row) for row in descendants],
            "next_cursor": None,
            "root_id": root.id,
        },
    )


PARTICIPANT_READ_HANDLERS = MappingProxyType(
    {
        "frontend.participants.list": participants_list,
        "frontend.participants.get": participants_get,
        "frontend.participants.tree": participants_tree,
    }
)

__all__ = ["PARTICIPANT_READ_HANDLERS", "participant_to_wire"]
