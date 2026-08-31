"""Core domain types shared by the daemon, the MCP server and the CLI."""

from __future__ import annotations

import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum

from theater.transcript_identity import TRANSCRIPT_IDENTITY_LOST_CODE


class Tier(StrEnum):
    """How a participant reached the registry. See init_idea_grilled.md §6."""

    SPAWNED = "spawned"  # daemon created the pane; identity by construction
    ADOPTED = "adopted"  # pre-existing pane, self-registered
    EXTERNAL = "external"  # no pane at all; emit-only, never addressable


class Status(StrEnum):
    IDLE = "idle"
    WORKING = "working"
    AWAITING_INPUT = "awaiting_input"
    DEAD = "dead"


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
    # Live-only alias; never persisted. Use the id for cross-time targeting — names recycle.
    name: str | None = None

    @property
    def addressable(self) -> bool:
        """External participants can call out but can never be called.

        This is a consequence of MCP having no server-initiated turn primitive:
        inbound delivery needs a tmux pane, and External has none.
        """
        return self.tier is not Tier.EXTERNAL and self.status is not Status.DEAD

    @property
    def live_pid(self) -> int | None:
        """The launch process, withheld once this participant is known dead.

        A pid outlives the process it named, and the operating system is free
        to hand the number to something else. Anything that asks the operating
        system about a dead participant's pid is therefore asking about
        whatever inherited it — and for transcript correlation, where a wrong
        answer attributes a live sibling's session to a dead row, that is a
        mis-attribution no later evidence can undo.

        Read what this is carefully: `DEAD` is a conclusion the registry has
        already reached, not a kernel fact, so this narrows the window rather
        than closing it. A process that exited a moment ago is still `RUNNING`
        here until the observer notices, and a caller that cached the number
        keeps it until its source is rebuilt. What remains is a race between
        the reaper and pid reuse, in which the recycled pid must also land on a
        codex holding a rollout for the same working directory.

        Two different bounds on what that could cost, and only the first is
        strong. A *watcher* cannot steal a location another live watcher has
        already bound: exact-against-exact keeps the incumbent
        (`daemon/observer.py`, `_accept_attachment`), so only an unbound
        location is at risk. A *history* read is not bound by that at all —
        `read_transcript` and recall open short-lived sources that never
        consult the binding table — so within the race a recycled pid can
        serve an incumbent's transcript to somebody else. Closing that needs
        process identity established where the pane is owned, in the daemon,
        rather than a pid handed to an adapter.

        Not on the wire: `to_dict` is built from the dataclass fields, and this
        is a reading of one of them rather than another one.
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
        )


class JobState(StrEnum):
    """Only RUNNING is non-terminal; the rest are where a job comes to rest.

    `timeout` is deliberately absent: it is what `await` returns when the
    caller stops waiting, not something that happens to the job.
    """

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
    caller_id: str
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
        )


class TheaterError(Exception):
    """Base for errors that should reach a client as a structured code."""

    code = "error"


class NotFound(TheaterError):
    code = "not_found"


class BadRequest(TheaterError):
    code = "bad_request"


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
    """The pane is showing an approval or trust modal, and typing would answer it.

    A `send` is delivered by pasting into the target's tmux pane. At an
    approval prompt, Enter is a button press, not text, so an injected prompt
    can auto-approve a tool call the human never saw (`docs/v1.6_observation.md`
    lines 88-91). This gate refuses a send when a fresh `capture-pane` reads
    `approval` or `trust` at `high` confidence.

    Temporary, unlike `NotAddressable` (permanent) and `StaleTarget` (the
    address is dead): the modal is a transient screen the human will dismiss,
    and the caller should retry. A stuck pane reads `working` or `unknown`,
    never `approval`, so it stays reachable — which is why the gate reads a
    fresh capture rather than the stored `Status`. `AWAITING_INPUT` is a
    display hint tuned to accept false negatives; using it as a control signal
    would make a stuck `WORKING` pane unreachable (`docs/v1.7_hardening.md`
    lines 188-191, 223-228).

    This gate also removes the only mechanism by which an agent could answer
    a child's approval dialog. That is intended: the alternative is a parent
    auto-approving a tool call the human never saw.
    """

    code = "awaiting_decision"


class Busy(TheaterError):
    code = "busy"


class StaleTarget(TheaterError):
    """The pane on record is no longer the participant we think it is.

    Distinct from `NotAddressable`, which is a permanent property of a tier.
    This one says the address was right once and has since gone stale: the
    pane closed, was respawned under a new process, or the harness exited and
    left a shell sitting at the prompt. The distinction matters to a caller,
    because retrying is pointless in a way that a `Busy` retry is not.
    """

    code = "stale_target"


class NotYourChild(TheaterError):
    """A kill was attempted on a participant the caller did not spawn.

    Covers a sibling, a parent, a stranger, or a grandchild — anything whose
    ``parent_id`` is not the caller's id. Distinct from a plain ``NotFound``,
    which says the id does not exist at all: this one says it does, and it is
    not yours to kill. The caller learns nothing it did not already know from
    ``list_participants``, because the check runs after the record is fetched.
    """

    code = "not_your_child"


class NoSelfKill(TheaterError):
    """A kill was attempted on the caller's own participant id.

    Separate from ``NotYourChild`` because the failure is different: the id
    exists and the lineage is known, but self-termination through this path
    is refused. An agent that needs to stop should exit its own process.
    """

    code = "no_self_kill"


class NameTaken(TheaterError):
    """A rename was attempted to a name another participant already holds.

    Distinct from ``BadRequest`` (the name is malformed) and ``NotFound``
    (the target does not exist): the name is valid and the target is real,
    but it belongs to someone else. The caller should pick a different name
    or rename the holder first.
    """

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
