"""Job state machine for spawn → await → result.
Only ``running`` is non-terminal; ``timeout`` is an await return value, not a job state. ``await``
wakes on ANY terminal job; touches commit with the result in one transaction.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import insert, update

from theater.constants.daemon import (
    RPC_DEFAULT_MAX_WAIT_SECONDS,
    TOUCH_HASH_MAX_FILE_BYTES,
    TOUCH_HASH_MAX_JOB_BYTES,
)
from theater.daemon.blob import BlobHash, BlobHashState, blob_hash
from theater.daemon.events.publication import job_event, next_revision
from theater.daemon.schema import jobs as jobs_table
from theater.daemon.schema import touch as touch_table
from theater.daemon.store import Store
from theater.daemon.touch_paths import normalize_touch_path
from theater.harness.base import EventPath
from theater.models import Job, JobKind, JobState, now

logger = logging.getLogger("theater.jobs")

#: Re-exported for callers that think of these as job vocabulary (they live in theater.models).
__all__ = [
    "Job",
    "JobKind",
    "JobManager",
    "JobState",
    "TouchAccumulator",
]


#: How long to wait for a job to finish if the caller does not specify.
DEFAULT_MAX_WAIT = RPC_DEFAULT_MAX_WAIT_SECONDS
STRUCTURED_PARSED = "parsed"
STRUCTURED_UNAVAILABLE = "unavailable"
_RAW_UNSET = object()


@dataclass
class TouchAccumulator:
    """Per-job set of file paths, with ``sha_before`` captured on first sight.
    Rehashed at job end for ``sha_after``: equal means touched, different means modified, null means
    deleted.
    """

    #: The working directory the job runs in; EventPath paths are resolved against this.
    cwd: str
    #: path -> sha_before, captured once on first sight; re-hashing would overwrite.
    _before: dict[str, BlobHash] = field(default_factory=dict)
    #: All paths in first-seen order; preserved so touch rows have deterministic order.
    _paths: list[str] = field(default_factory=list)
    #: mode per path, last write wins; the final action left the file in its state.
    _mode: dict[str, str] = field(default_factory=dict)
    #: Total regular-file bytes hashed at first observation.
    _before_bytes: int = 0

    def observe(self, paths: tuple[EventPath, ...]) -> None:
        """Record paths from one event. Hashes new paths immediately."""
        for ep in paths:
            path = normalize_touch_path(self.cwd, ep.path)
            if path is None:
                continue
            if path not in self._before:
                self._paths.append(path)
                remaining = max(0, TOUCH_HASH_MAX_JOB_BYTES - self._before_bytes)
                outcome = blob_hash(
                    Path(self.cwd) / path,
                    max_bytes=min(TOUCH_HASH_MAX_FILE_BYTES, remaining),
                )
                self._before[path] = outcome
                if outcome.state is BlobHashState.HASHED:
                    self._before_bytes += outcome.size
            self._mode[path] = ep.mode

    def rows(self, job_handle: str) -> list[dict]:
        """The touch rows for this job, with ``sha_after`` computed now (None if deleted)."""
        result = []
        after_bytes = 0
        for path in self._paths:
            safe_path = normalize_touch_path(self.cwd, path)
            # A symlink may have escaped since observation; never hash it.
            if safe_path is None:
                after = BlobHash(BlobHashState.UNAVAILABLE, reason="unsafe_path")
            else:
                remaining = max(0, TOUCH_HASH_MAX_JOB_BYTES - after_bytes)
                after = blob_hash(
                    Path(self.cwd) / safe_path,
                    max_bytes=min(TOUCH_HASH_MAX_FILE_BYTES, remaining),
                )
                if after.state is BlobHashState.HASHED:
                    after_bytes += after.size
            before = self._before[path]
            if BlobHashState.UNAVAILABLE in (before.state, after.state):
                logger.debug(
                    "recording unavailable touch hash for %s/%s: before=%s after=%s",
                    self.cwd,
                    path,
                    before.reason,
                    after.reason,
                )
            result.append(
                {
                    "job_handle": job_handle,
                    "path": path,
                    "mode": self._mode[path],
                    "sha_before": before.digest,
                    "sha_after": after.digest,
                    "sha_before_error": (
                        (before.reason or "unavailable")
                        if before.state is BlobHashState.UNAVAILABLE
                        else None
                    ),
                    "sha_after_error": (
                        (after.reason or "unavailable")
                        if after.state is BlobHashState.UNAVAILABLE
                        else None
                    ),
                }
            )
        return result

    def __bool__(self) -> bool:
        """Whether any paths were observed; false means the plain autocommit finish path."""
        return bool(self._paths)


class JobManager:
    """Owns job state and the asyncio events that ``await`` waits on.

    Events are in-memory only, correctly: after a restart no await is left in flight.
    """

    def __init__(self, store: Store):
        self.store = store
        self._events: dict[str, asyncio.Event] = {}
        #: Awaits in flight, keyed by opaque token so concurrent awaits can be torn down.
        self._waits: dict[object, tuple[str, frozenset[str]]] = {}
        #: Per-job path accumulators; a job with no cwd (CLI spawn, no target) gets no accumulator.
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
        response_format: str | None = None,
        actor_client_id: str | None = None,
        actor_participant_id: str | None = None,
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
            response_format=response_format,
            actor_client_id=actor_client_id,
            actor_participant_id=actor_participant_id,
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
        """Feed ``Event.paths`` into this job's accumulator; no-op without one (no cwd, or
        finished).
        """
        acc = self._accumulators.get(handle)
        if acc is not None and paths:
            acc.observe(paths)

    def attach_touch_accumulator(self, handle: str, *, cwd: str) -> bool:
        """Attach a queued job's path accumulator at dispatch time; return whether one is attached.
        A pending followup must receive no touches, results, or rescue attention until it
        dispatches.
        """
        job = self.store.get_job(handle)
        if job is None or job.state != JobState.RUNNING:
            return False
        if handle not in self._accumulators:
            self._accumulators[handle] = TouchAccumulator(cwd=cwd)
        return True

    def replace_touch_accumulator(self, handle: str, *, cwd: str) -> bool:
        """Retarget a pre-dispatch spawn accumulator after workspace preparation."""
        job = self.store.get_job(handle)
        if job is None or job.state != JobState.RUNNING:
            return False
        self._accumulators[handle] = TouchAccumulator(cwd=cwd)
        return True

    def get(self, handle: str) -> Job | None:
        return self.store.get_job(handle)

    def active_count(self) -> int:
        """Count of jobs whose persisted state is ``running``."""
        return self.store.active_job_count()

    def finish(
        self,
        handle: str,
        *,
        state: JobState,
        result: str | None = None,
        error_code: str | None = None,
        raw_result: str | object | None = _RAW_UNSET,
    ) -> Job | None:
        job = self.store.get_job(handle)
        if job is None:
            return None
        if job.state != JobState.RUNNING:
            # Already finished; event may linger after a daemon restart — set it so await wakes.
            event = self._events.pop(handle, None)
            if event:
                event.set()
            self._accumulators.pop(handle, None)
            return job

        acc = self._accumulators.get(handle)
        finished_at = now()
        structured_result, structured_status = self._structured_values(
            job,
            state=state,
            result=result,
            error_code=error_code,
            raw_result=raw_result,
        )
        if acc:
            # Write job result and touches in one transaction; store connection is autocommit.
            self._finish_with_touches(
                handle,
                state=str(state),
                result=result,
                error_code=error_code,
                finished_at=finished_at,
                response_format=job.response_format,
                structured_result=structured_result,
                structured_status=structured_status,
                touches=acc.rows(handle),
            )
        else:
            self.store.finish_job(
                handle,
                state=str(state),
                result=result,
                error_code=error_code,
                finished_at=finished_at,
                response_format=job.response_format,
                structured_result=structured_result,
                structured_status=structured_status,
            )

        self._accumulators.pop(handle, None)

        # Wake waiters then drop the event; await_jobs short-circuits on terminal state.
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

    def notify_committed_finish(self, job: Job) -> None:
        """Wake local consumers after another domain writer commits job completion."""
        self._accumulators.pop(job.handle, None)
        event = self._events.pop(job.handle, None)
        if event:
            event.set()
        self.store.bus_append(
            "job.finished",
            from_id=job.target_id,
            to_id=job.caller_id,
            payload={
                "handle": job.handle,
                "state": str(job.state),
                "error_code": job.error_code,
            },
        )
        logger.info("job %s finished: %s", job.handle, job.state)

    def _structured_values(
        self,
        job: Job,
        *,
        state: JobState,
        result: str | None,
        error_code: str | None,
        raw_result: str | object | None,
    ) -> tuple[str | None, str | None]:
        if job.response_format is None:
            return None, None
        if str(state) != str(JobState.DONE) or error_code is not None:
            return None, STRUCTURED_UNAVAILABLE
        if raw_result is _RAW_UNSET:
            candidate = result or ""
        elif raw_result is None:
            return None, STRUCTURED_UNAVAILABLE
        else:
            if not isinstance(raw_result, str):
                return None, STRUCTURED_UNAVAILABLE
            candidate = raw_result
        try:
            json.loads(candidate)
        except (ValueError, RecursionError):
            return None, STRUCTURED_UNAVAILABLE
        return candidate, STRUCTURED_PARSED

    def _finish_with_touches(
        self,
        handle: str,
        *,
        state: str,
        result: str | None,
        error_code: str | None,
        finished_at: float | None,
        response_format: str | None,
        structured_result: str | None,
        structured_status: str | None,
        touches: list[dict],
    ) -> None:
        """Write the job result and its touch rows in one transaction.

        The write unit keeps the result, touch rows, and public event atomic.
        """
        with self.store.write_unit() as unit:
            unit.connection.execute(
                update(jobs_table)
                .where(jobs_table.c.handle == handle)
                .values(
                    state=state,
                    result=result,
                    error_code=error_code,
                    finished_at=finished_at,
                    response_format=response_format,
                    structured_result=structured_result,
                    structured_status=structured_status,
                )
            )
            if touches:
                unit.connection.execute(insert(touch_table), touches)
            current = self.store.get_job(handle, connection=unit.connection)
            assert current is not None
            self.store.journal.append_group(
                unit,
                [
                    job_event(
                        current,
                        revision=next_revision(self.store, unit.connection),
                        recorded_at=current.finished_at or now(),
                    )
                ],
            )

    @property
    def wait_graph(self) -> dict[str, set[str]]:
        """Who is blocked on whom, right now. Rebuilt per call, never cached.

        In-memory on purpose: persisting would resurrect edges a restart has already ended.
        """
        graph: dict[str, set[str]] = {}
        for caller, targets in self._waits.values():
            graph.setdefault(caller, set()).update(targets)
        return graph

    @contextmanager
    def waiting(self, caller_id: str | None, target_ids: list[str]) -> Iterator[None]:
        """Hold caller -> target edges for one await; no-op without both ends (e.g. the CLI)."""
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
        """Wait until ANY requested job is terminal or timeout; return all states in input order.

        Unknown handles are skipped here only because the RPC layer already rejects them.
        """
        # Partition into terminal (return immediately) vs running (wait); any terminal = no wait.
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
                # Lost the event (daemon restart); fresh unset event so finish() can set it.
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
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        return [j for j in (self.store.get_job(h) for h in handles) if j is not None]
