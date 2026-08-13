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
        # Migration shim: older daemons persisted 'starting' before STARTING was
        # removed. A row from such a database loads as IDLE, since a participant
        # that never left 'starting' was functionally idle. This can be deleted
        # in a later version once no database can contain the old value.
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
