"""Job state machine for spawn → await → result.

A job is a unit of work sent to a participant. `spawn` creates a job; the
observer detects turn-end and captures the assistant text as the result;
`await` blocks the caller's MCP request until the job finishes or times out.

States:
    running     the target is working
    done        turn-end detected, result captured
    crashed     the participant died before finishing
    killed      the participant was killed by the caller or a human
    timeout     the await ceiling expired (not a final state — the job
                may still finish later; the caller just stopped waiting)

Only `running` is non-terminal. Once a job reaches `done`, `crashed`, or
`killed`, it stays there. `timeout` is not stored as a job state — it is
a return value of `await` that says "I stopped waiting", not a change to
the job itself.

The await mechanism
-------------------
`await` blocks the caller's MCP request only — the daemon and every other
participant continue. This is the key insight from the spec: the reply is
the return value of a tool call the agent already made. No inbound-reply
channel is needed.

Implementation: `await_jobs` creates an asyncio.Event per job, then waits
on them with a timeout. The observer calls `job_finished` when it detects
turn-end, which sets the event. The caller wakes up, reads the result, and
returns it as the MCP tool response.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum

from theater.daemon.store import Store
from theater.models import now

logger = logging.getLogger("theater.jobs")


class JobState(StrEnum):
    RUNNING = "running"
    DONE = "done"
    CRASHED = "crashed"
    KILLED = "killed"


class JobKind(StrEnum):
    SPAWN = "spawn"
    SEND = "send"


@dataclass(frozen=True, slots=True)
class Job:
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


#: How long to wait for a job to finish if the caller does not specify.
DEFAULT_MAX_WAIT = 60.0


class JobManager:
    """Owns job state and the asyncio events that `await` waits on.

    The store persists job state for restart recovery (phase 7). The events
    are in-memory only — they do not survive a daemon restart, which is
    correct: a restarted daemon has no observer connected yet, so any
    in-flight await would need to re-poll anyway.
    """

    def __init__(self, store: Store):
        self.store = store
        self._events: dict[str, asyncio.Event] = {}
        self._results: dict[str, str] = {}

    def create(
        self,
        *,
        handle: str,
        caller_id: str,
        target_id: str | None,
        kind: str,
        prompt: str | None = None,
    ) -> Job:
        job = Job(
            handle=handle,
            caller_id=caller_id,
            target_id=target_id,
            kind=kind,
            prompt=prompt,
            state=JobState.RUNNING,
            result=None,
            error_code=None,
            created_at=now(),
            finished_at=None,
        )
        self.store.create_job(job)
        self._events[handle] = asyncio.Event()
        self.store.bus_append(
            "job.created",
            from_id=caller_id,
            to_id=target_id,
            payload={"handle": handle, "kind": str(kind)},
        )
        return job

    def get(self, handle: str) -> Job | None:
        return self.store.get_job(handle)

    def finish(
        self,
        handle: str,
        *,
        state: JobState,
        result: str | None = None,
        error_code: str | None = None,
    ) -> Job | None:
        job = self.store.get_job(handle)
        if job is None:
            return None
        if job.state != JobState.RUNNING:
            # Already finished. The event may still need setting if the
            # daemon was restarted and lost the in-memory event.
            event = self._events.get(handle)
            if event:
                event.set()
            return job
        self.store.finish_job(
            handle,
            state=str(state),
            result=result,
            error_code=error_code,
            finished_at=now(),
        )
        if result:
            self._results[handle] = result
        event = self._events.get(handle)
        if event:
            event.set()
        self.store.bus_append(
            "job.finished",
            from_id=job.target_id,
            to_id=job.caller_id,
            payload={
                "handle": handle,
                "state": str(state),
                "error_code": error_code,
            },
        )
        logger.info("job %s finished: %s", handle, state)
        return self.store.get_job(handle)

    def list_for_caller(self, caller_id: str) -> list[Job]:
        return self.store.list_jobs_for_caller(caller_id)

    async def await_jobs(
        self, handles: list[str], max_wait: float = DEFAULT_MAX_WAIT
    ) -> list[Job]:
        """Wait for jobs to finish, up to max_wait seconds.

        Returns the current state of each job. Jobs that are still running
        when the timeout expires are returned with state=running — the caller
        decides whether to re-await.
        """
        events = []
        for h in handles:
            job = self.store.get_job(h)
            if job is None:
                continue
            if job.state != JobState.RUNNING:
                continue
            event = self._events.get(h)
            if event is None:
                # Lost the event (daemon restart). Create a new one and
                # let the caller poll. It will be set if the job is already
                # finished.
                event = asyncio.Event()
                self._events[h] = event
                if job.state != JobState.RUNNING:
                    event.set()
            events.append(event)

        if events:
            done, pending = await asyncio.wait(
                [asyncio.create_task(e.wait()) for e in events],
                timeout=max_wait,
            )
            for task in pending:
                task.cancel()

        return [j for j in (self.store.get_job(h) for h in handles) if j is not None]
