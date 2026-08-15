"""Core domain types shared by the daemon, the MCP server and the CLI."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum


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
    cwd: str | None = None
    branch: str | None = None
    session_id: str | None = None
    parent_id: str | None = None
    pid: int | None = None
    status: Status = Status.IDLE
    last_activity: float = field(default_factory=now)
    created_at: float = field(default_factory=now)
    # Live-only: populated by the Registry for participants that are alive,
    # None for dead ones. Never persisted — the name is regenerated when the
    # daemon restarts, and a dead participant's name is released so a later
    # participant can recycle it. The id is the stable identity for as long
    # as the row is retained (dead rows are eventually deleted by retention
    # GC); use it, not the name, for any targeting that spans time or has
    # destructive consequences, because a recycled name can identify a successor.
    name: str | None = None

    @property
    def addressable(self) -> bool:
        """External participants can call out but can never be called.

        This is a consequence of MCP having no server-initiated turn primitive:
        inbound delivery needs a tmux pane, and External has none.
        """
        return self.tier is not Tier.EXTERNAL and self.status is not Status.DEAD

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tier"] = str(self.tier)
        d["status"] = str(self.status)
        d["addressable"] = self.addressable
        return d

    @classmethod
    def from_row(cls, row) -> Participant:
        # Migration shim: older daemons persisted 'starting' before that status
        # was removed. A row with it loads as IDLE — a participant that never
        # left 'starting' was functionally idle.
        raw_status = row["status"]
        status = Status.IDLE if raw_status == "starting" else Status(raw_status)
        return cls(
            id=row["id"],
            harness=row["harness"],
            tier=Tier(row["tier"]),
            tmux_pane=row["tmux_pane"],
            cwd=row["cwd"],
            branch=row["branch"],
            session_id=row["session_id"],
            parent_id=row["parent_id"],
            pid=row["pid"],
            status=status,
            last_activity=row["last_activity"],
            created_at=row["created_at"],
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
