"""Core domain types shared by the daemon, the MCP server and the CLI."""

from __future__ import annotations

import time
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum

from theater.transcript_identity import TRANSCRIPT_IDENTITY_LOST_CODE


class Tier(StrEnum):
    """How a participant reached the registry."""

    SPAWNED = "spawned"  # daemon created the pane; identity by construction
    ADOPTED = "adopted"  # pre-existing pane, self-registered
    EXTERNAL = "external"  # no pane at all; emit-only, never addressable


class Status(StrEnum):
    IDLE = "idle"
    WORKING = "working"
    AWAITING_INPUT = "awaiting_input"
    DEAD = "dead"


class ParticipantOrigin(StrEnum):
    SPAWNED = "spawned"
    ADOPTED = "adopted"
    EXTERNAL = "external"


class ControlOwnerKind(StrEnum):
    LOCAL_OPERATOR = "local_operator"
    PARTICIPANT = "participant"


class PublicOperationState(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    UNCERTAIN = "uncertain"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class WorkspaceOwnershipKind(StrEnum):
    THEATER = "theater"
    FRONTEND = "frontend"
    BORROWED = "borrowed"


class WorkspaceState(StrEnum):
    CREATING = "creating"
    ACTIVE = "active"
    DELETING = "deleting"
    REMOVED = "removed"
    RECONCILE = "reconcile"


class WorkspaceUsageHolderKind(StrEnum):
    RESERVATION = "reservation"
    PARTICIPANT = "participant"


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now() -> float:
    return time.time()


@dataclass(slots=True)
class Participant:
    id: str = field(default_factory=new_id)
    harness: str = "unknown"
    tier: Tier = Tier.EXTERNAL
    tmux_pane: str | None = None
    tmux_server_identity: str | None = None
    termination_reason: str | None = None
    termination_incident: str | None = None
    terminated_at: float | None = None
    cwd: str | None = None
    branch: str | None = None
    session_id: str | None = None
    #: Provenance of session_id: exact evidence or heuristic. Persisted across restarts.
    session_correlation: str | None = None
    #: Namespace for heuristic transcript discovery; distinct domains cannot share a transcript.
    transcript_domain: str | None = None
    #: Last location accepted by attachment policy — a pin, not proof of identity.
    transcript_location: str | None = None
    #: Persisted resume floor: predecessor's stream position at last safe pre-launch. None for cold.
    resume_floor: str | None = None
    #: Opaque durable cursor prepared by a harness source.
    source_checkpoint: str | None = None
    resumed_from_id: str | None = None
    parent_id: str | None = None
    pid: int | None = None
    status: Status = Status.IDLE
    last_activity: float = field(default_factory=now)
    created_at: float = field(default_factory=now)
    #: Durable, user-facing summary of this participant's purpose.
    description: str | None = None
    origin: ParticipantOrigin | None = None
    control_owner_kind: ControlOwnerKind | None = None
    control_owner_id: str | None = None
    control_revision: int = 0
    workspace_id: str | None = None
    # Live-only alias; never persisted. Use the id for cross-time targeting — names recycle.
    name: str | None = None

    @property
    def addressable(self) -> bool:
        """Historical participant columns never prove a current physical route."""
        return False

    @property
    def live_pid(self) -> int | None:
        """The launch process, withheld once this participant is known dead.
        A recycled pid can misattribute a live sibling's transcript; ``DEAD`` is the registry's
        conclusion, so this narrows the reuse race rather than closing it. Not on the wire.
        """
        return None if self.status is Status.DEAD else self.pid

    def to_dict(self) -> dict:
        d = asdict(self)
        # Internal provenance used by the observer; not part of protocol v1.
        d.pop("session_correlation", None)
        d.pop("transcript_domain", None)
        d.pop("transcript_location", None)
        d.pop("resume_floor", None)
        d.pop("source_checkpoint", None)
        d.pop("resumed_from_id", None)
        d["tier"] = str(self.tier)
        d["status"] = str(self.status)
        d["origin"] = str(self.origin) if self.origin is not None else None
        d["control_owner_kind"] = (
            str(self.control_owner_kind) if self.control_owner_kind is not None else None
        )
        d["addressable"] = self.addressable
        return d

    @classmethod
    def from_row(cls, row) -> Participant:
        # Migration shim: older daemons persisted 'starting'; loads as IDLE.
        raw_status = row["status"]
        status = Status.IDLE if raw_status == "starting" else Status(raw_status)
        mapping = row._mapping if hasattr(row, "_mapping") else row
        return cls(
            id=mapping["id"],
            harness=mapping["harness"],
            tier=Tier(mapping["tier"]),
            tmux_pane=mapping["tmux_pane"],
            tmux_server_identity=mapping["tmux_server_identity"],
            termination_reason=mapping["termination_reason"],
            termination_incident=mapping["termination_incident"],
            terminated_at=mapping["terminated_at"],
            cwd=mapping["cwd"],
            branch=mapping["branch"],
            session_id=mapping["session_id"],
            session_correlation=mapping["session_correlation"],
            transcript_domain=mapping["transcript_domain"],
            transcript_location=mapping["transcript_location"],
            resume_floor=mapping["resume_floor"],
            source_checkpoint=mapping["source_checkpoint"],
            resumed_from_id=mapping["resumed_from_id"],
            parent_id=mapping["parent_id"],
            pid=mapping["pid"],
            status=status,
            last_activity=mapping["last_activity"],
            created_at=mapping["created_at"],
            description=mapping["description"],
            origin=ParticipantOrigin(mapping["origin"]),
            control_owner_kind=ControlOwnerKind(mapping["control_owner_kind"]),
            control_owner_id=mapping["control_owner_id"],
            control_revision=mapping["control_revision"],
            workspace_id=mapping["workspace_id"],
        )


class JobState(StrEnum):
    """Only RUNNING is non-terminal; ``timeout`` is an await result, not a job state."""

    RUNNING = "running"
    DONE = "done"
    CRASHED = "crashed"
    KILLED = "killed"


class JobKind(StrEnum):
    SPAWN = "spawn"
    SEND = "send"


@dataclass(frozen=True, slots=True)
class Job:
    """A unit of work sent to a participant. See theater.daemon.jobs."""

    handle: str
    caller_id: str | None
    target_id: str | None
    kind: str
    prompt: str | None
    state: str
    result: str | None
    error_code: str | None
    created_at: float
    finished_at: float | None
    response_format: str | None = None
    structured_result: str | None = None
    structured_status: str | None = None
    actor_client_id: str | None = None
    actor_participant_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "handle": self.handle,
            "caller_id": self.caller_id,
            "target_id": self.target_id,
            "kind": str(self.kind),
            "prompt": self.prompt,
            "state": str(self.state),
            "result": self.result,
            "error_code": self.error_code,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "response_format": self.response_format,
            "structured_result": self.structured_result,
            "structured_status": self.structured_status,
            "actor_client_id": self.actor_client_id,
            "actor_participant_id": self.actor_participant_id,
        }

    @classmethod
    def from_row(cls, row) -> Job:
        return cls(
            handle=row["handle"],
            caller_id=row["caller_id"],
            target_id=row["target_id"],
            kind=row["kind"],
            prompt=row["prompt"],
            state=row["state"],
            result=row["result"],
            error_code=row["error_code"],
            created_at=row["created_at"],
            finished_at=row["finished_at"],
            response_format=row["response_format"],
            structured_result=row["structured_result"],
            structured_status=row["structured_status"],
            actor_client_id=row["actor_client_id"],
            actor_participant_id=row["actor_participant_id"],
        )


@dataclass(frozen=True, slots=True)
class ProviderRecord:
    provider_id: str
    selector: str
    kind: str
    credential_verifier: str
    configuration_version: int
    capabilities: tuple[str, ...]
    limits: Mapping[str, object]
    generation: int
    last_report_revision: int | None
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class TerminalBindingRecord:
    participant_id: str
    provider_id: str
    provider_generation: int
    terminal_id: str
    terminal_incarnation: str
    occupant_evidence: Mapping[str, object]
    health: str
    report_revision: int
    created_at: float
    updated_at: float
    process_facts: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class PublicOperationRecord:
    operation_id: str
    kind: str
    actor_client_id: str
    actor_participant_id: str | None
    target_ids: tuple[str, ...]
    state: str
    phase: str
    created_at: float
    updated_at: float
    control_operation_id: str | None = None
    job_handle: str | None = None
    result: object | None = None
    error_code: str | None = None
    error: Mapping[str, object] | None = None
    dispatch_provider_id: str | None = None
    dispatch_provider_generation: int | None = None
    dispatch_terminal_id: str | None = None
    dispatch_terminal_incarnation: str | None = None
    dispatch_terminal_occupant_evidence: Mapping[str, object] | None = None
    dispatch_terminal_process_facts: Mapping[str, object] | None = None
    dispatch_backend_generation: int | None = None
    dispatch_native_session_id: str | None = None
    dispatch_native_turn_id: str | None = None
    settled_at: float | None = None


@dataclass(frozen=True, slots=True)
class LaunchReservationRecord:
    operation_id: str
    participant_id: str
    provider_id: str
    adapter: str
    phase: str
    launch_facts: Mapping[str, object]
    artifact_refs: tuple[str, ...]
    created_at: float
    updated_at: float
    workspace_usage_id: str | None = None
    dispatch_marker: str | None = None


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    client_id: str
    key: str
    method: str
    payload_digest: str
    created_at: float
    operation_id: str | None = None
    response: object | None = None
    settled_at: float | None = None
    retain_until: float | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    workspace_id: str
    ownership_kind: str
    owner_id: str
    path: str
    state: str
    created_at: float
    updated_at: float
    canonical_repository_root: str | None = None
    branch: str | None = None
    resolved_base_commit: str | None = None
    name: str | None = None
    #: The accepted spawn that owns a Theater-created workspace's exact intent.
    creation_operation_id: str | None = None
    deletion_operation_id: str | None = None
    deletion_token: str | None = None
    deletion_prior_state: str | None = None
    cleanup_force: bool | None = None
    cleanup_delete_branch: bool | None = None
    cleanup_force_branch: bool | None = None
    cleanup_result: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceUsageRecord:
    usage_id: str
    workspace_id: str
    holder_kind: str
    holder_id: str
    acquired_at: float
    released_at: float | None = None
    release_reason: str | None = None


@dataclass(frozen=True, slots=True)
class JournalEventRecord:
    kind: str
    entity_id: str
    entity_revision: int
    payload: Mapping[str, object]
    recorded_at: float


class TheaterError(Exception):
    """Base for errors that should reach a client as a structured code."""

    code = "error"
    # Optional diagnostic detail for a refusal event; the wire error code stays stable.
    refusal_reason: str | None = None


class NotFound(TheaterError):
    code = "not_found"


class BadRequest(TheaterError):
    code = "bad_request"


class PluginAuthenticationFailed(TheaterError):
    """A sidecar credential is malformed, revoked, stale, or invalid."""

    code = "plugin_auth_failed"


class CapabilityDenied(TheaterError):
    """An authenticated sidecar lacks the capability required by an operation."""

    code = "capability_denied"

    def __init__(self, *, required: str, granted: tuple[str, ...] | list[str]) -> None:
        self.required = required
        self.granted = tuple(granted)
        self.details = {"required": self.required, "granted": list(self.granted)}
        super().__init__(
            f"plugin operation requires capability {required!r}; "
            f"granted capabilities are {', '.join(self.granted) or '(none)'}"
        )


class NotAddressable(TheaterError):
    code = "not_addressable"


class HumanPresent(TheaterError):
    code = "human_present"


class TranscriptUntrusted(TheaterError):
    """A send would require attributing a transcript that is not yet trusted."""

    code = "transcript_untrusted"


class TranscriptIdentityLost(TheaterError):
    """A trusted transcript pin lost identity and must be rebound by an operator."""

    code = TRANSCRIPT_IDENTITY_LOST_CODE


class AwaitingDecision(TheaterError):
    """The pane shows an approval or trust modal, and typing would answer it.
    Temporary; read from a fresh high-confidence capture, not stored ``Status``, so pasted prompts
    can never auto-approve a tool call the human never saw (agents cannot answer children's modals).
    """

    code = "awaiting_decision"


class Busy(TheaterError):
    code = "busy"


class StaleTarget(TheaterError):
    """The pane on record is no longer the participant we think it is.
    Unlike ``NotAddressable`` (a tier property) the address was once right; unlike ``Busy``,
    retrying is pointless.
    """

    code = "stale_target"


class NotYourChild(TheaterError):
    """A kill was attempted on a participant the caller did not spawn.
    Unlike ``NotFound``, the id exists; checked after the fetch, so it reveals nothing
    ``list_participants`` does not.
    """

    code = "not_your_child"


class NoSelfKill(TheaterError):
    """A kill was attempted on the caller's own id; an agent that must stop should exit itself."""

    code = "no_self_kill"


class NameTaken(TheaterError):
    """A rename was attempted to a valid name another participant already holds."""

    code = "name_taken"


def normalize_participant_description(value: str) -> str | None:
    """Normalize a participant description or explain why it is unsafe to store."""
    from theater.constants.limits import PARTICIPANT_DESCRIPTION_MAX_CODEPOINTS

    if not isinstance(value, str):
        raise BadRequest("description must be a string or null")
    normalized = value.strip()
    if not normalized:
        return None
    if any(
        character in "\r\n\u0085\u2028\u2029" or unicodedata.category(character) == "Cc"
        for character in normalized
    ):
        raise BadRequest("description must be one line and contain no control characters")
    if len(normalized) > PARTICIPANT_DESCRIPTION_MAX_CODEPOINTS:
        raise BadRequest(
            "description must be at most "
            f"{PARTICIPANT_DESCRIPTION_MAX_CODEPOINTS} Unicode codepoints; shorten it"
        )
    return normalized
