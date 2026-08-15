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
on them with a timeout. The observer calls `JobManager.finish` when it detects
turn-end, which sets the event. The caller wakes up, reads the result, and
returns it as the MCP tool response.

The multi-handle semantic
-------------------------
When multiple known handles are awaited, `await_jobs` returns as soon as ANY
requested job becomes terminal — not when all of them do. This lets a fan-out
caller react to the first result without blocking on the slowest sibling.
If any requested job is already terminal at call entry, the method returns
immediately. The returned list always contains the current state of every
requested handle (including still-running jobs), in input order. Timeout
behavior is unchanged: on timeout, every job's current state is returned,
running jobs still running.

Unknown handles are silently skipped at this layer — the RPC handler
(``jobs.await``) rejects them with ``bad_request`` before reaching here, so
``await_jobs`` only ever sees handles that exist.

The touch accumulator
---------------------
`recall` records which files each job touched, keyed by content hash so a
later query can detect drift. A path is hashed when it is first seen during
the job (that is `sha_before`), and every path is hashed again at job end
(that is `sha_after`). So something must accumulate per-job paths across
events, and that something is `TouchAccumulator`, living here on the
`JobManager`. The observer feeds it by calling `observe_paths` for each
event that carries `Event.paths`; at job end, `finish` writes the
accumulated rows in the same transaction as the job result, so a job whose
result committed but whose touches did not is impossible.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import insert, update

from theater.daemon.blob import blob_sha
from theater.daemon.schema import jobs as jobs_table
from theater.daemon.schema import touch as touch_table
from theater.daemon.store import Store
from theater.harness.base import EventPath
from theater.models import Job, JobKind, JobState, now

logger = logging.getLogger("theater.jobs")

#: Re-exported for callers that think of these as job vocabulary. They live in
#: theater.models so the store can build a Job without importing this module.
__all__ = [
    "Job",
    "JobKind",
    "JobManager",
    "JobState",
    "TouchAccumulator",
]


#: How long to wait for a job to finish if the caller does not specify.
DEFAULT_MAX_WAIT = 150.0


@dataclass
class TouchAccumulator:
    """Per-job set of file paths, with ``sha_before`` captured on first sight.

    A path is hashed exactly once — the first time the job sees it — and that
    hash is ``sha_before``. At job end, every path is hashed again for
    ``sha_after``. The pair is what makes drift detection work: same before
    and after means the file was touched but not changed, different means it
    was modified, and a null end means it was deleted.

    Lives on the ``JobManager``, one per running job, created in ``create``
    and consumed in ``finish``. The observer feeds it by calling
    ``observe_paths`` with the ``Event.paths`` it extracts from each event;
    until the plugins fill those in (Wave 2), the accumulator legitimately
    collects nothing and ``finish`` writes no touch rows.
    """

    #: The working directory the job runs in. Paths in EventPath are
    #: repo-relative; this is how they resolve to a real file for hashing.
    cwd: str
    #: path -> sha_before, captured the first time the path is seen. A path
    #: seen again does not re-hash: the before state is first sight, not last
    #: sight, and re-hashing would overwrite it with a mid-job hash.
    _before: dict[str, str | None] = field(default_factory=dict)
    #: All paths seen, in first-seen order. Preserved so touch rows have a
    #: deterministic order — non-deterministic order makes test assertions
    #: flaky and debugging harder.
    _paths: list[str] = field(default_factory=list)
    #: mode per path, last write wins. A path read then written records
    #: "write"; written then read records "read". The final action is the
    #: one that left the file in the state the next job sees.
    _mode: dict[str, str] = field(default_factory=dict)

    def observe(self, paths: tuple[EventPath, ...]) -> None:
        """Record paths from one event. Hashes new paths immediately."""
        for ep in paths:
            if ep.path not in self._before:
                self._paths.append(ep.path)
                self._before[ep.path] = blob_sha(Path(self.cwd) / ep.path)
            self._mode[ep.path] = ep.mode

    def rows(self, job_handle: str) -> list[dict]:
        """The touch rows for this job, with ``sha_after`` computed now.

        Called at job end. Every path is hashed again, including ones whose
        ``sha_before`` was None (the file was created during the job): if the
        file still exists, ``sha_after`` is its hash; if it was deleted
        during the job, ``sha_after`` is None.
        """
        result = []
        for path in self._paths:
            sha_after = blob_sha(Path(self.cwd) / path)
            result.append(
                {
                    "job_handle": job_handle,
                    "path": path,
                    "mode": self._mode[path],
                    "sha_before": self._before[path],
                    "sha_after": sha_after,
                }
            )
        return result

    def __bool__(self) -> bool:
        """Whether any paths have been observed.

        Checked by ``JobManager.finish`` to decide whether to open a
        transaction for touches or take the plain finish path. A false
        accumulator means no touch rows to write, so the job result goes
        through the store's autocommit path as before.
        """
        return bool(self._paths)


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
        #: Awaits in flight, keyed by an opaque token so two concurrent awaits
        #: from the same caller can be torn down independently. Read as a
        #: graph by `wait_graph`.
        self._waits: dict[object, tuple[str, frozenset[str]]] = {}
        #: Per-job path accumulators. Created in `create`, consumed in
        #: `finish`. A job whose target is None (CLI spawn, no target) gets
        #: no accumulator — no working directory to resolve paths against.
        self._accumulators: dict[str, TouchAccumulator] = {}

    def create(
        self,
        *,
        handle: str,
        caller_id: str,
        target_id: str | None,
        kind: str,
        prompt: str | None = None,
        cwd: str | None = None,
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
        if cwd is not None:
            self._accumulators[handle] = TouchAccumulator(cwd=cwd)
        self.store.bus_append(
            "job.created",
            from_id=caller_id,
            to_id=target_id,
            payload={"handle": handle, "kind": str(kind)},
        )
        return job

    def observe_paths(self, handle: str, paths: tuple[EventPath, ...]) -> None:
        """Feed ``Event.paths`` into the accumulator for this job.

        Called by the observer for each event that carries paths. A job with
        no accumulator (no cwd, or already finished) is a no-op: there is
        nothing to resolve paths against, and a job whose finish already
        committed has had its accumulator consumed.
        """
        acc = self._accumulators.get(handle)
        if acc is not None and paths:
            acc.observe(paths)

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
            # Already finished. The event may still be here after a daemon
            # restart lost the original and `await_jobs` made a fresh one.
            event = self._events.pop(handle, None)
            if event:
                event.set()
            self._accumulators.pop(handle, None)
            return job

        acc = self._accumulators.pop(handle, None)
        if acc:
            # Write job result and touches in one transaction so a result
            # without its touches is impossible. The store's connection is
            # autocommit, so we open an explicit transaction here.
            self._finish_with_touches(
                handle,
                state=str(state),
                result=result,
                error_code=error_code,
                finished_at=now(),
                touches=acc.rows(handle),
            )
        else:
            self.store.finish_job(
                handle,
                state=str(state),
                result=result,
                error_code=error_code,
                finished_at=now(),
            )

        # Wake anyone waiting, then drop the event: the job is terminal, so
        # `await_jobs` short-circuits on state from here on. Keeping it would
        # grow the dict one entry per prompt ever sent.
        event = self._events.pop(handle, None)
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

    def _finish_with_touches(
        self,
        handle: str,
        *,
        state: str,
        result: str | None,
        error_code: str | None,
        finished_at: float | None,
        touches: list[dict],
    ) -> None:
        """Write the job result and its touch rows in one transaction.

        Uses a fresh connection from the store's engine rather than the
        store's long-lived autocommit connection, because SQLAlchemy 2.0
        does not allow ``conn.begin()`` on a connection that is already in
        an autobegun transaction (which an AUTOCOMMIT connection always is
        after its first use). The engine's ``connect`` event listener
        re-applies WAL and foreign-key pragmas to every fresh connection,
        so the transactional write sees the same settings as the autocommit
        path. The alternative — changing the store's connection model —
        would affect every other caller, which is out of scope.
        """
        with self.store.engine.begin() as conn:
            conn.execute(
                update(jobs_table)
                .where(jobs_table.c.handle == handle)
                .values(
                    state=state,
                    result=result,
                    error_code=error_code,
                    finished_at=finished_at,
                )
            )
            if touches:
                conn.execute(insert(touch_table), touches)

    @property
    def wait_graph(self) -> dict[str, set[str]]:
        """Who is blocked on whom, right now. Rebuilt per call, never cached.

        In-memory on purpose. A wait is a call in flight, not a fact about the
        world: a daemon that restarts has no callers left waiting on it, so
        persisting this would resurrect edges that no longer exist.
        """
        graph: dict[str, set[str]] = {}
        for caller, targets in self._waits.values():
            graph.setdefault(caller, set()).update(targets)
        return graph

    @contextmanager
    def waiting(self, caller_id: str | None, target_ids: list[str]) -> Iterator[None]:
        """Hold caller -> target edges in the wait graph for one await.

        A no-op without both ends. The CLI awaits as `"cli"`, which nothing
        can send to, so it can never be the target of an edge and never part
        of a loop.
        """
        if not caller_id or not target_ids:
            yield
            return
        token = object()
        self._waits[token] = (caller_id, frozenset(target_ids))
        try:
            yield
        finally:
            self._waits.pop(token, None)

    async def await_jobs(self, handles: list[str], max_wait: float = DEFAULT_MAX_WAIT) -> list[Job]:
        """Wait until ANY of the requested jobs becomes terminal, or timeout.

        Returns the current state of every requested handle, in input order.
        If any requested job is already terminal at call entry, returns
        immediately. Otherwise, waits until the first requested job finishes
        (or ``max_wait`` expires), then returns all current states. Jobs that
        are still running when the timeout expires are returned with
        state=running — the caller decides whether to re-await.

        Unknown handles are silently skipped here; the RPC layer rejects them
        with ``bad_request`` before calling this method, so callers that go
        through the socket never see a silent drop.
        """
        # Partition into already-terminal (return immediately) vs. running
        # (need to wait). An already-terminal job at entry means we return
        # right away without waiting at all.
        events: list[asyncio.Event] = []
        for h in handles:
            job = self.store.get_job(h)
            if job is None:
                continue
            if job.state != JobState.RUNNING:
                # At least one requested job is already terminal — return now.
                return [j for j in (self.store.get_job(h) for h in handles) if j is not None]
            event = self._events.get(h)
            if event is None:
                # Lost the event (daemon restart). The job is running, so a
                # fresh unset event is right: finish() will set it, or we
                # time out and the caller re-awaits.
                event = asyncio.Event()
                self._events[h] = event
            events.append(event)

        if events:
            tasks = [asyncio.create_task(e.wait()) for e in events]
            try:
                await asyncio.wait(
                    tasks,
                    timeout=max_wait,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in tasks:
                    task.cancel()
                # Await cancelled tasks so they don't leak as "Task was
                # destroyed but it is pending" warnings. Cancelled tasks
                # raise CancelledError on await; suppress them all.
                for task in tasks:
                    with suppress(asyncio.CancelledError):
                        await task

        return [j for j in (self.store.get_job(h) for h in handles) if j is not None]
