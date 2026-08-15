"""The garbage-collection engine and its daemon loop.

The SQLite database at ``~/.theater/theater.db`` grows without bound.
Measured on a real machine over 4.26 days: 32.05 MB total, of which the
``bus`` table was 30.20 MB (94.2%) growing at 7.1 MB/day — about 2.6 GB/year.
This module is the sweep that bounds it.

The sweep runs in four phases, in this order:

1. **Stale running jobs** — mark abandoned ones finished, excluding handles
   the running daemon still knows about. This must come first so the jobs
   phase can then consider them. See MF1 below.
2. **Jobs + touch** — delete finished jobs older than ``jobs_days`` along
   with their touch rows, in one transaction per batch so a crash can never
   orphan touch rows from their job.
3. **Participants** — the three-clause gated delete. After the jobs phase,
   so a participant whose last job just went becomes eligible in the same
   sweep.
4. **Bus** — delete rows older than ``bus_days`` (except ``send.refused``),
   then trim ``send.refused`` to the newest ``refused_cap`` rows.

**MF1 — never delete a running job.** ``JobManager.finish()`` looks the job
up and does ``if job is None: return None`` *before* setting the asyncio
Event that ``await_sessions`` is blocked on. So if the sweep deletes a job
row that is still ``running``, the agent finishing its turn cannot wake its
caller: the caller hangs until its own timeout, with no explanation. That is
the worst failure this feature could introduce. The predicate
``finished_at IS NOT NULL AND finished_at < cutoff`` self-protects, because
``finished_at`` is NULL while a job runs and ``NULL < x`` is never true in
SQL. Do not filter on ``created_at`` anywhere in the job sweep.

**MF3 — the third participant clause is a safety rail, not a nicety.**
``rails.py`` walks ``parent_id`` upward with ``store.get_participant`` and
does not filter out dead rows. Deleting a participant in the middle of a
lineage chain terminates the walk early, depth is under-counted, and a spawn
the cap should have refused is allowed. The rail fails *open* — it stops
protecting without any error. So the participant sweep needs all three
clauses, and the third is not optional.

Follows ``recall.py``'s precedent for a query module outside ``Store``:
imports the tables from ``theater.daemon.schema`` and executes against
``store.conn``. These queries are not ``Store`` methods.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import delete, select, text, update

from theater.config import RetentionSection
from theater.daemon.schema import bus, jobs, participants, touch
from theater.daemon.store import Store
from theater.models import now

logger = logging.getLogger("theater.gc")

#: Seconds per day, factored out so the arithmetic reads at the call site.
_DAY = 86400.0


@dataclass(frozen=True, slots=True)
class SweepResult:
    """Counts of rows actually deleted (or, for ``running_marked``, updated).

    Every field defaults to zero so a no-op sweep returns all-zero without
    the caller having to handle ``None``.
    """

    bus: int = 0
    jobs: int = 0
    touch: int = 0
    participants: int = 0
    running_marked: int = 0


async def sweep(
    store: Store,
    retention: RetentionSection,
    *,
    live_handles: frozenset[str] = frozenset(),
) -> SweepResult:
    """Run all four GC phases in order, returning per-phase row counts.

    ``sweep`` is async for one reason only: between batches it calls
    ``await asyncio.sleep(0)`` to yield the event loop, so a long sweep
    does not starve the daemon's status polling or await wakes. There is
    no I/O wait — the Store is synchronous on purpose.

    ``live_handles`` is the set of handles the running daemon's
    ``JobManager`` still holds in ``self._events``. A stale-running sweep
    must never mark one of those crashed behind the manager's back — that
    would desynchronise it from a live await. The daemon passes
    ``frozenset(daemon.jobs._events)``.
    """
    result = SweepResult(
        bus=0,
        jobs=0,
        touch=0,
        participants=0,
        running_marked=0,
    )

    cutoff_jobs = now() - retention.jobs_days * _DAY
    cutoff_bus = now() - retention.bus_days * _DAY
    stale_cutoff = now() - retention.stale_running_days * _DAY

    # Phase 1: stale running jobs.
    marked = _sweep_stale_running(
        store, stale_cutoff, retention.batch, live_handles
    )
    result = SweepResult(
        bus=result.bus,
        jobs=result.jobs,
        touch=result.touch,
        participants=result.participants,
        running_marked=marked,
    )
    await asyncio.sleep(0)

    # Phase 2: jobs + touch.
    jobs_deleted, touch_deleted = await _sweep_jobs_and_touch(
        store, cutoff_jobs, retention.batch
    )
    result = SweepResult(
        bus=result.bus,
        jobs=jobs_deleted,
        touch=touch_deleted,
        participants=result.participants,
        running_marked=result.running_marked,
    )

    # Phase 3: participants — after jobs, so a participant whose last job
    # just went becomes eligible in the same sweep.
    part_deleted = _sweep_participants(store, retention.batch)
    result = SweepResult(
        bus=result.bus,
        jobs=result.jobs,
        touch=result.touch,
        participants=part_deleted,
        running_marked=result.running_marked,
    )
    await asyncio.sleep(0)

    # Phase 4: bus.
    bus_deleted = await _sweep_bus(
        store, cutoff_bus, retention.batch, retention.refused_cap
    )
    result = SweepResult(
        bus=bus_deleted,
        jobs=result.jobs,
        touch=result.touch,
        participants=result.participants,
        running_marked=result.running_marked,
    )

    # The WAL was measured at 4.12 MB live and grows with churn; checkpointing
    # costs approximately nothing (measured ~0 ms).
    store.conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))

    return result


def _sweep_stale_running(
    store: Store,
    stale_cutoff: float,
    batch: int,
    live_handles: frozenset[str],
) -> int:
    """Mark abandoned running jobs as crashed/abandoned (MF1).

    A job orphaned in ``running`` (daemon killed mid-turn) has
    ``finished_at = NULL`` forever and becomes immortal — it would accumulate
    and also pin its participant against the job-gated participant delete.
    This marks them finished so the jobs phase can then consider them.

    Never touches a job whose handle is in ``live_handles``: the running
    daemon's ``JobManager`` holds in-memory ``asyncio.Event`` for those,
    and marking one crashed behind the manager's back would desynchronise
    it from a live await.
    """
    stmt = (
        select(jobs.c.handle)
        .where(jobs.c.state == "running")
        .where(jobs.c.created_at < stale_cutoff)
        .limit(batch)
    )
    rows = store.conn.execute(stmt).fetchall()
    handles = [r[0] for r in rows if r[0] not in live_handles]
    if not handles:
        return 0
    store.conn.execute(
        update(jobs)
        .where(jobs.c.handle.in_(handles))
        .values(state="crashed", finished_at=now(), error_code="abandoned")
    )
    return len(handles)


async def _sweep_jobs_and_touch(
    store: Store, cutoff: float, batch: int
) -> tuple[int, int]:
    """Delete finished jobs older than the cutoff along with their touch rows.

    Filters on ``finished_at IS NOT NULL AND finished_at < cutoff`` — never on
    ``created_at``. A running job has ``finished_at = NULL`` and
    ``NULL < x`` is never true, so the sweep can never delete a job whose
    caller is still waiting on it (MF1).

    Each batch selects up to ``batch`` handles, deletes the matching touch
    rows and the job rows in one transaction (via ``store.engine.begin()``)
    so a crash can never leave touch rows orphaned from their job. The loop
    repeats until a batch comes back short.
    """
    total_jobs = 0
    total_touch = 0
    while True:
        # Select the next batch of handles to delete. We need the handles
        # both to find the touch rows and to delete the job rows.
        stmt = (
            select(jobs.c.handle)
            .where(jobs.c.finished_at.is_not(None))
            .where(jobs.c.finished_at < cutoff)
            .limit(batch)
        )
        rows = store.conn.execute(stmt).fetchall()
        handles = [r[0] for r in rows]
        if not handles:
            break

        # One transaction so touch and job rows go together. The store's
        # long-lived connection is AUTOCOMMIT and cannot begin() on itself
        # after first use — see JobManager._finish_with_touches for the same
        # precedent.
        with store.engine.begin() as conn:
            touch_result = conn.execute(
                delete(touch).where(touch.c.job_handle.in_(handles))
            )
            job_result = conn.execute(
                delete(jobs).where(jobs.c.handle.in_(handles))
            )
        total_touch += touch_result.rowcount
        total_jobs += job_result.rowcount
        await asyncio.sleep(0)
        if len(handles) < batch:
            break
    return total_jobs, total_touch


def _sweep_participants(store: Store, batch: int) -> int:
    """Delete dead participants that nothing references (MF3).

    Participants are gated, never aged: a dead participant becomes eligible
    only once the job sweep has removed every job that references it. The
    three clauses each protect a different reference:

    1. ``target_id`` — a job still in flight against this participant.
    2. ``caller_id`` — a job record that names this participant as the
       caller. If deleted, ``recall.py``'s INNER join from touch to jobs
       would drop rows whose ``caller_id`` is this participant.
    3. ``parent_id`` — another participant's lineage chain. ``rails.py``
       walks ``parent_id`` upward with ``get_participant`` and does not
       filter out dead rows. Deleting a participant in the middle of a
       chain terminates the walk early, depth is under-counted, and a spawn
       the cap should have refused is allowed. The rail fails *open*.
       **Do not delete the third clause** — the next person will read it as
       redundant, and it is not.
    """
    stmt = (
        delete(participants)
        .where(participants.c.status == "dead")
        .where(
            participants.c.id.not_in(
                select(jobs.c.target_id).where(jobs.c.target_id.is_not(None))
            )
        )
        .where(participants.c.id.not_in(select(jobs.c.caller_id)))
        .where(
            participants.c.id.not_in(
                select(participants.c.parent_id).where(
                    participants.c.parent_id.is_not(None)
                )
            )
        )
    )
    result = store.conn.execute(stmt)
    return result.rowcount


async def _sweep_bus(
    store: Store, cutoff: float, batch: int, refused_cap: int
) -> int:
    """Delete old bus rows, then trim ``send.refused`` to the cap.

    ``send.refused`` events are the only record of a refused send
    (``_refuse_send`` deliberately writes no job row), so they are exempt
    from the age TTL and capped by row count instead.
    """
    total = 0
    # Age-based deletion: everything old except send.refused.
    # SQLAlchemy Core's delete() does not support .limit(), so select the
    # ids first and then delete by id — the same subquery pattern the jobs
    # phase uses.
    while True:
        sub = (
            select(bus.c.id)
            .where(bus.c.ts < cutoff)
            .where(bus.c.kind != "send.refused")
            .limit(batch)
        )
        ids = [r[0] for r in store.conn.execute(sub).fetchall()]
        if not ids:
            break
        result = store.conn.execute(delete(bus).where(bus.c.id.in_(ids)))
        total += result.rowcount
        await asyncio.sleep(0)
        if len(ids) < batch:
            break

    # Cap-based trimming of send.refused: keep the newest refused_cap rows.
    count_stmt = select(bus.c.id).where(bus.c.kind == "send.refused")
    refused_ids = [r[0] for r in store.conn.execute(count_stmt).fetchall()]
    if len(refused_ids) > refused_cap:
        # IDs are autoincrement, so higher id = newer. Keep the newest.
        to_delete = sorted(refused_ids)[:-refused_cap] if refused_cap > 0 else refused_ids
        if to_delete:
            # Batch the deletion to avoid a single huge statement.
            for i in range(0, len(to_delete), batch):
                chunk = to_delete[i : i + batch]
                result = store.conn.execute(
                    delete(bus).where(bus.c.id.in_(chunk))
                )
                total += result.rowcount
                await asyncio.sleep(0)

    return total


def vacuum(store: Store) -> None:
    """Run ``VACUUM`` to shrink the database file on disk.

    **Synchronous and blocking on purpose** — never call this from the
    daemon's event loop. It is for an explicit user command only.

    Deleting rows does *not* shrink the file: measured, deleting 94% of the
    bus table moved it from 32.05 MB to 32.16 MB — it *grew*, because of the
    WAL. Only VACUUM shrinks it, by rewriting the whole file. A user who
    runs GC and sees no change on disk will otherwise report it as broken.

    VACUUM cannot run inside a transaction. The store's connection is
    AUTOCOMMIT, so this works — but VACUUM acquires an exclusive lock for
    the duration of the rewrite, which is why it must never run on the
    daemon's loop.
    """
    store.conn.execute(text("VACUUM"))
