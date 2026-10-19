"""Shared participant, job, provider, and workspace event projections."""

from __future__ import annotations

import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sqlalchemy import Connection, select

from theater.daemon.control_projection import project_control_action
from theater.daemon.events.reader import StateReadError
from theater.daemon.job_results import structured_job_result
from theater.daemon.participants.resume import resume_state
from theater.daemon.schema import participant_runtime_bindings, participants
from theater.daemon.transcript_projection import transcript_identity_projection
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
)
from theater.provenance import is_trusted_provenance


@dataclass(frozen=True, slots=True)
class ParticipantProjectionFacts:
    """Cached, non-I/O facts that can safely accompany one durable row."""

    presence: str
    terminal_route: Mapping[str, object] | None
    native_route: Mapping[str, object] | None
    actions: Mapping[str, Mapping[str, object]]
    addressable: bool
    transcript_identity: Mapping[str, object] | None = None


type ParticipantProjectionResolver = Callable[
    [
        Participant,
        TerminalBindingRecord | None,
        Mapping[str, object] | None,
        bool,
        Connection | None,
        tuple[str, int] | None,
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
        transactional_presence_snapshot: (
            Callable[[str, TerminalBindingRecord | None, bool], object] | None
        ) = None,
        terminal_projection: Callable[[TerminalBindingRecord], Mapping[str, object]],
        route_for: Callable[[str, RuntimeCapability], object],
        provider_health: Callable[[str, int], str],
        action_projection: Callable[..., Mapping[str, object]] | None = None,
        native_route: Callable[
            [Participant, Mapping[str, object] | None], Mapping[str, object] | None
        ]
        | None = None,
        transactional_route_for: Callable[[str, RuntimeCapability, Connection], object]
        | None = None,
        transcript_identity: Callable[[Participant], Mapping[str, object]] | None = None,
    ) -> None:
        self._presence_snapshot = presence_snapshot
        self._transactional_presence_snapshot = transactional_presence_snapshot
        self._terminal_projection = terminal_projection
        self._route_for = route_for
        self._provider_health = provider_health
        self._action_projection = action_projection
        self._native_route = native_route
        self._transactional_route_for = transactional_route_for
        self._transcript_identity = transcript_identity

    def __call__(
        self,
        participant: Participant,
        binding: TerminalBindingRecord | None,
        durable_native_route: Mapping[str, object] | None,
        transactional: bool,
        connection: Connection | None = None,
        projected_online_provider: tuple[str, int] | None = None,
    ) -> ParticipantProjectionFacts:
        terminal_route = _project_terminal_route(
            binding,
            self._terminal_projection,
            preserve_pending_health=transactional,
        )
        native_route = self._project_native_route(participant, durable_native_route)
        presence, presence_detail = self._presence(
            participant.id,
            binding=binding,
            transactional=transactional,
            projected_online_provider=projected_online_provider,
        )
        actions = self._actions(
            participant,
            terminal_route,
            native_route,
            presence,
            presence_detail,
            connection=connection if transactional else None,
            projected_online_provider=projected_online_provider,
        )
        addressable = participant.status is not Status.DEAD and any(
            action["route_available"] is True for action in actions.values()
        )
        transcript_identity: Mapping[str, object] | None = None
        if self._transcript_identity is not None:
            try:
                current_identity = self._transcript_identity(participant)
            except Exception:
                current_identity = None
            if isinstance(current_identity, Mapping):
                transcript_identity = dict(current_identity)
        return ParticipantProjectionFacts(
            presence=presence,
            terminal_route=terminal_route,
            native_route=native_route,
            actions=actions,
            addressable=addressable,
            transcript_identity=transcript_identity,
        )

    def _presence(
        self,
        participant_id: str,
        *,
        binding: TerminalBindingRecord | None,
        transactional: bool,
        projected_online_provider: tuple[str, int] | None,
    ) -> tuple[str, str | None]:
        try:
            snapshot = (
                self._transactional_presence_snapshot(
                    participant_id,
                    binding,
                    binding is not None
                    and projected_online_provider
                    == (binding.provider_id, binding.provider_generation),
                )
                if transactional and self._transactional_presence_snapshot is not None
                else self._presence_snapshot(participant_id)
            )
            state = getattr(snapshot, "state", None)
            value = getattr(state, "value", state)
            if getattr(snapshot, "reason", None) == "unregistered":
                return "unknown", getattr(snapshot, "reason", None)
            return (
                value if isinstance(value, str) and value else "unknown",
                getattr(snapshot, "reason", None),
            )
        except Exception:
            return "unknown", "presence snapshot failed"

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
        presence_detail: str | None,
        *,
        connection: Connection | None,
        projected_online_provider: tuple[str, int] | None,
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
            route_available = _route_available(
                route,
                terminal_route,
                native_route,
                provider_health=self._provider_health,
                projected_online_provider=projected_online_provider,
            )
            if self._action_projection is None:
                actions[capability.value] = project_control_action(
                    route,
                    capability,
                    route_available=route_available,
                    alive=participant.status is not Status.DEAD,
                    presence=presence,
                    presence_detail=presence_detail,
                )
            else:
                actions[capability.value] = dict(
                    self._action_projection(
                        participant.id,
                        capability,
                        route=route,
                        route_available=route_available,
                        alive=participant.status is not Status.DEAD,
                        presence=presence,
                        presence_detail=presence_detail,
                        connection=connection,
                    )
                )
        return actions


def _participant_projection_facts(
    store: object,
    participant: Participant,
    binding: TerminalBindingRecord | None,
    native_route: Mapping[str, object] | None,
    connection: Connection,
    *,
    projection: ParticipantProjectionResolver | None,
    transactional: bool,
    projected_online_provider: tuple[str, int] | None,
) -> ParticipantProjectionFacts | None:
    resolver = projection or _configured_participant_projection(store)
    if resolver is None:
        return None
    try:
        value = resolver(
            participant,
            binding,
            native_route,
            transactional,
            connection,
            projected_online_provider,
        )
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
    projected_online_provider: tuple[str, int] | None,
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
        return health == "online" or (
            health == "reconciling" and projected_online_provider == (provider_id, generation)
        )
    if _route_flag(route, "is_native"):
        return native_route is not None and native_route.get("health") in {
            ConnectionHealth.CONNECTED.value,
            ConnectionHealth.DEGRADED.value,
        }
    # Legacy pane fields are migration data, not a current provider delivery route.
    return False


def _participant_projection(
    store,
    participant: Participant,
    connection: Connection,
    *,
    name: str | None = None,
    projection: ParticipantProjectionResolver | None = None,
    transactional: bool = False,
    projected_online_provider: tuple[str, int] | None = None,
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
        projected_online_provider=projected_online_provider,
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
    transcript_identity = transcript_identity_projection(participant)
    if facts is not None and facts.transcript_identity is not None:
        transcript_identity = dict(facts.transcript_identity)
    if participant.status is Status.DEAD:
        live_peers = [
            Participant.from_row(row._mapping)
            for row in connection.execute(
                select(participants).where(participants.c.status != Status.DEAD.value)
            )
        ]
        participant_resume_state = resume_state(participant, live_peers)
    else:
        participant_resume_state = "live"
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
        "transcript_identity": transcript_identity,
        "resume_state": participant_resume_state,
        "created_at": participant.created_at,
        "last_activity": participant.last_activity,
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
        "result": structured_job_result(job),
        "error": error,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "structured_status": job.structured_status,
        "raw_result": job.result,
        "error_code": job.error_code,
        "response_format": job.response_format,
    }


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


def _token_revision(token: str) -> int:
    try:
        return max(0, int(token.split("-", 1)[0]))
    except ValueError:
        return 0
