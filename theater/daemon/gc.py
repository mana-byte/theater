"""The garbage-collection engine and its daemon loop.

The SQLite database grows without bound unless retained data is swept.
Measured on a real machine over 4.26 days: 32.05 MB total, of which the
``bus`` table was 30.20 MB (94.2%) growing at 7.1 MB/day — about 2.6 GB/year.
This module is the sweep that bounds it.

The sweep runs in six phases, in this order:

1. **Stale running jobs** — mark abandoned ones finished, excluding handles
   the running daemon still knows about. This must come first so the jobs
   phase can then consider them. See MF1 below.
2. **Jobs + touch** — delete finished jobs older than ``jobs_days`` along
   with their touch rows, in one transaction per batch so a crash can never
   orphan touch rows from their job.
3. **Participants** — the three-clause gated delete. After the jobs phase,
   so a participant whose last job just went becomes eligible in the same
   sweep.
4. **Participant artifacts** — remove participant roots only after their rows
   are deleted, then clean orphaned metadata and roots in bounded work.
5. **scratchpad** — delete expired global entries in bounded batches.
6. **Bus** — delete rows older than ``bus_days`` (except ``send.refused`` and
   active transcript-identity-loss audit rows), then trim ``send.refused`` to
   the newest ``refused_cap`` rows.
7. **Event journal** — prune complete expired groups without resetting sequence.

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
import json
import logging
from dataclasses import dataclass

from sqlalchemy import delete, func, or_, select, text, update

from theater.config import RetentionSection
from theater.constants import SECONDS_PER_DAY
from theater.constants.daemon import (
    BUS_KIND_SEND_REFUSED,
    TMUX_RESTART_TERMINATION_REASON,
    TRANSCRIPT_AUDIT_KINDS,
)
from theater.daemon import workers
from theater.daemon.artifacts import (
    baseline_artifacts,
    cleanup_orphan_paths,
    cleanup_orphan_recorded,
    cleanup_participant,
    orphan_paths,
)
from theater.daemon.events.publication import job_event, next_revision, tombstone_event
from theater.daemon.persistence.repositories.journal import MAX_EVENTS_PER_TRANSACTION
from theater.daemon.schema import (
    bus,
    jobs,
    orchestration_events,
    participants,
    touch,
    workspace_usages,
)
from theater.daemon.store import Store
from theater.models import Job, now
from theater.transcript_identity import TRANSCRIPT_IDENTITY_LOST_CODE

logger = logging.getLogger("theater.gc")


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
    scratchpad: int = 0


async def sweep(
    store: Store,
    retention: RetentionSection,
    *,
    live_handles: frozenset[str] = frozenset(),
) -> SweepResult:
    """Run all seven GC phases in order, returning legacy per-phase row counts.

    ``sweep`` yields between batches and offloads filesystem cleanup, so a
    long sweep does not starve the daemon's status polling or await wakes.
    Store access remains synchronous and on the daemon's event-loop thread.

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
        scratchpad=0,
    )

    cutoff_jobs = now() - retention.jobs_days * SECONDS_PER_DAY
    cutoff_bus = now() - retention.bus_days * SECONDS_PER_DAY
    stale_cutoff = now() - retention.stale_running_days * SECONDS_PER_DAY

    # Phase 1: stale running jobs.
    marked = _sweep_stale_running(store, stale_cutoff, retention.batch, live_handles)
    result = SweepResult(
        bus=result.bus,
        jobs=result.jobs,
        touch=result.touch,
        participants=result.participants,
        running_marked=marked,
        scratchpad=result.scratchpad,
    )
    await asyncio.sleep(0)

    # Phase 2: jobs + touch.
    jobs_deleted, touch_deleted = await _sweep_jobs_and_touch(store, cutoff_jobs, retention.batch)
    result = SweepResult(
        bus=result.bus,
        jobs=jobs_deleted,
        touch=touch_deleted,
        participants=result.participants,
        running_marked=result.running_marked,
        scratchpad=result.scratchpad,
    )

    # Phase 3: participants — after jobs so newly-eligible ones are deleted in the same sweep.
    part_deleted, deleted_participant_ids = await _sweep_participants(
        store, cutoff_jobs, retention.batch
    )
    result = SweepResult(
        bus=result.bus,
        jobs=result.jobs,
        touch=result.touch,
        participants=part_deleted,
        running_marked=result.running_marked,
        scratchpad=result.scratchpad,
    )
    await asyncio.sleep(0)

    # Phase 4: participant artifacts and orphaned credentials.
    await _sweep_artifact_orphans(store, retention.batch, exclude=deleted_participant_ids)
    store.cleanup_receipt_tokens()
    store.cleanup_channel_credentials()
    store.cleanup_mcp_plugin_credentials()
    await asyncio.sleep(0)

    result = SweepResult(
        bus=result.bus,
        jobs=result.jobs,
        touch=result.touch,
        participants=result.participants,
        running_marked=result.running_marked,
        scratchpad=result.scratchpad,
    )

    # Phase 5: scratchpad — physical expiry follows the stored global TTL.
    kv_deleted = await _sweep_scratchpad(store, retention.batch)
    result = SweepResult(
        bus=result.bus,
        jobs=result.jobs,
        touch=result.touch,
        participants=result.participants,
        running_marked=result.running_marked,
        scratchpad=kv_deleted,
    )

    # Phase 6: bus.
    bus_deleted = await _sweep_bus(store, cutoff_bus, retention.batch, retention.refused_cap)
    result = SweepResult(
        bus=bus_deleted,
        jobs=result.jobs,
        touch=result.touch,
        participants=result.participants,
        running_marked=result.running_marked,
        scratchpad=result.scratchpad,
    )

    await _sweep_journal(store, now() - 7 * SECONDS_PER_DAY, retention.batch)

    # WAL checkpoint: ~0 ms cost, prevents unbounded WAL growth.
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
        select(jobs)
        .where(jobs.c.state == "running")
        .where(jobs.c.created_at < stale_cutoff)
        .limit(min(batch, MAX_EVENTS_PER_TRANSACTION))
    )
    rows = store.conn.execute(stmt).fetchall()
    handles = [str(row.handle) for row in rows if row.handle not in live_handles]
    if not handles:
        return 0
    timestamp = now()
    with store.write_unit() as unit:
        current_rows = unit.connection.execute(
            select(jobs)
            .where(jobs.c.handle.in_(handles))
            .where(jobs.c.state == "running")
            .where(jobs.c.created_at < stale_cutoff)
            .order_by(jobs.c.handle)
        ).all()
        if not current_rows:
            return 0
        current_handles = [str(row.handle) for row in current_rows]
        unit.connection.execute(
            update(jobs)
            .where(jobs.c.handle.in_(current_handles))
            .values(state="crashed", finished_at=timestamp, error_code="abandoned")
        )
        first = next_revision(store, unit.connection)
        finished = [
            Job.from_row(row._mapping)
            for row in unit.connection.execute(
                select(jobs).where(jobs.c.handle.in_(current_handles)).order_by(jobs.c.handle)
            )
        ]
        store.journal.append_group(
            unit,
            [
                job_event(job, revision=first + index, recorded_at=timestamp)
                for index, job in enumerate(finished)
            ],
        )
    return len(current_handles)


async def _sweep_jobs_and_touch(store: Store, cutoff: float, batch: int) -> tuple[int, int]:
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
    event_batch = min(batch, MAX_EVENTS_PER_TRANSACTION)
    while True:
        # Select the next batch of handles to delete (needed for both touch and job row deletion).
        stmt = (
            select(jobs.c.handle)
            .where(jobs.c.finished_at.is_not(None))
            .where(jobs.c.finished_at < cutoff)
            .limit(event_batch)
        )
        rows = store.conn.execute(stmt).fetchall()
        handles = [r[0] for r in rows]
        if not handles:
            break

        timestamp = now()
        with store.write_unit() as unit:
            retained_handles = [
                str(value)
                for value in unit.connection.execute(
                    select(jobs.c.handle)
                    .where(jobs.c.handle.in_(handles))
                    .where(jobs.c.finished_at.is_not(None))
                    .where(jobs.c.finished_at < cutoff)
                    .order_by(jobs.c.handle)
                ).scalars()
            ]
            if not retained_handles:
                continue
            touch_result = unit.connection.execute(
                delete(touch).where(touch.c.job_handle.in_(retained_handles))
            )
            job_result = unit.connection.execute(
                delete(jobs).where(jobs.c.handle.in_(retained_handles))
            )
            first = next_revision(store, unit.connection)
            store.journal.append_group(
                unit,
                [
                    tombstone_event(
                        "job.removed",
                        handle,
                        revision=first + index,
                        recorded_at=timestamp,
                    )
                    for index, handle in enumerate(retained_handles)
                ],
            )
        total_touch += int(touch_result.rowcount or 0)
        total_jobs += int(job_result.rowcount or 0)
        await asyncio.sleep(0)
        if len(handles) < event_batch:
            break
    return total_jobs, total_touch


async def _sweep_participants(
    store: Store, restart_cutoff: float, batch: int
) -> tuple[int, frozenset[str]]:
    """Delete dead participants that nothing references (MF3).

    Participants are gated, never aged except restart diagnoses: a tmux-reset
    row also waits through ``jobs_days`` from ``terminated_at``. Five guards
    protect references:

    1. ``target_id`` — a job still in flight against this participant.
    2. ``caller_id`` — a job record that names this participant as the
       caller. If deleted, ``recall.py``'s INNER join from touch to jobs
       would drop rows whose ``caller_id`` is this participant.
    3. ``resumed_from_id`` — a live or retained recovery successor's claim.
    4. ``parent_id`` — another participant's lineage chain. ``rails.py``
       walks ``parent_id`` upward with ``get_participant`` and does not
       filter out dead rows. Deleting a participant in the middle of a
       chain terminates the walk early, depth is under-counted, and a spawn
       the cap should have refused is allowed. The rail fails *open*.
       **Do not delete the fourth guard** — the next person will read it as
       redundant, and it is not.
    5. Active workspace usage — retained workspaces must not lose the identity
       of a participant whose execution still holds them.
    """
    total = 0
    deleted_ids: set[str] = set()
    after_id: str | None = None
    while True:
        filters = list(_eligible_participant_filters(restart_cutoff))
        if after_id is not None:
            filters.append(participants.c.id > after_id)
        stmt = select(participants.c.id).where(*filters).order_by(participants.c.id).limit(batch)
        ids = [row[0] for row in store.conn.execute(stmt).fetchall()]
        if not ids:
            break

        for participant_id in ids:
            participant = store.get_participant(participant_id)
            if participant is None:
                continue
            try:
                store.add_participant_artifacts(
                    participant_id,
                    baseline_artifacts(participant),
                )
                recorded = store.participant_artifacts(participant_id)
            except (OSError, TypeError, ValueError) as exc:
                logger.warning(
                    "participant artifact ownership for %s cannot be read; retaining "
                    "row for retry: %s",
                    participant_id,
                    exc,
                )
                continue

            deleted = _delete_participant_row(store, participant_id, restart_cutoff)
            if deleted:
                total += deleted
                deleted_ids.add(participant_id)
            else:
                logger.warning(
                    "participant %s became referenced before row deletion; retaining row",
                    participant_id,
                )
                continue
            try:
                failures = await workers.to_thread(
                    cleanup_participant,
                    participant,
                    recorded,
                    label="gc.participant_artifacts",
                )
            except Exception as exc:
                failures = (f"unexpected cleanup failure: {exc}",)
            if failures:
                logger.warning(
                    "participant artifact cleanup deferred for %s; retry on the next GC: %s",
                    participant_id,
                    "; ".join(failures),
                )
            else:
                store.delete_participant_artifacts(participant_id)
            await asyncio.sleep(0)

        after_id = ids[-1]
        if len(ids) < batch:
            break
    return total, frozenset(deleted_ids)


def _delete_participant_row(store: Store, participant_id: str, restart_cutoff: float) -> int:
    timestamp = now()
    with store.write_unit() as unit:
        deleted = unit.connection.execute(
            delete(participants)
            .where(participants.c.id == participant_id)
            .where(*_eligible_participant_filters(restart_cutoff))
        ).rowcount
        if deleted:
            store.journal.append_group(
                unit,
                [
                    tombstone_event(
                        "participant.removed",
                        participant_id,
                        revision=next_revision(store, unit.connection),
                        recorded_at=timestamp,
                    )
                ],
            )
    return int(deleted or 0)


async def _sweep_journal(store: Store, cutoff: float, batch: int) -> int:
    """Prune only whole expired groups; the allocator remains in ``meta``."""
    total = 0
    group_limit = max(1, min(batch, MAX_EVENTS_PER_TRANSACTION))
    while True:
        groups = store.conn.execute(
            select(
                orchestration_events.c.transaction_id,
                orchestration_events.c.ending_sequence,
                func.count().label("event_count"),
            )
            .group_by(
                orchestration_events.c.transaction_id,
                orchestration_events.c.ending_sequence,
            )
            .having(func.max(orchestration_events.c.recorded_at) < cutoff)
            .order_by(func.min(orchestration_events.c.sequence))
            .limit(group_limit)
        ).all()
        if not groups:
            return total
        transaction_ids: list[str] = []
        selected_events = 0
        row_limit = max(batch, MAX_EVENTS_PER_TRANSACTION)
        for row in groups:
            event_count = int(row.event_count)
            if transaction_ids and selected_events + event_count > row_limit:
                break
            transaction_ids.append(str(row.transaction_id))
            selected_events += event_count
        with store.write_unit() as unit:
            result = unit.connection.execute(
                delete(orchestration_events).where(
                    orchestration_events.c.transaction_id.in_(transaction_ids)
                )
            )
        total += int(result.rowcount or 0)
        await asyncio.sleep(0)
        if len(transaction_ids) == len(groups) and len(groups) < group_limit:
            return total


def _eligible_participant_filters(restart_cutoff: float):
    """Return the four reference guards for participant retention."""
    return (
        participants.c.status == "dead",
        or_(
            participants.c.termination_reason.is_(None),
            participants.c.termination_reason != TMUX_RESTART_TERMINATION_REASON,
            participants.c.terminated_at <= restart_cutoff,
        ),
        participants.c.id.not_in(select(jobs.c.target_id).where(jobs.c.target_id.is_not(None))),
        participants.c.id.not_in(select(jobs.c.caller_id)),
        participants.c.id.not_in(
            select(participants.c.resumed_from_id).where(
                participants.c.resumed_from_id.is_not(None)
            )
        ),
        participants.c.id.not_in(
            select(participants.c.parent_id).where(participants.c.parent_id.is_not(None))
        ),
        participants.c.id.not_in(
            select(workspace_usages.c.holder_id).where(
                workspace_usages.c.holder_kind == "participant",
                workspace_usages.c.released_at.is_(None),
            )
        ),
    )


async def _sweep_artifact_orphans(
    store: Store,
    batch: int,
    *,
    exclude: frozenset[str] = frozenset(),
) -> None:
    """Retry recorded artifacts and remove orphan participant roots."""
    retained_ids = frozenset(
        participant.id for participant in store.list_participants(include_dead=True)
    )
    owner_ids = [
        owner_id
        for owner_id in store.participant_artifact_owner_ids()
        if owner_id not in retained_ids and owner_id not in exclude
    ]
    for offset in range(0, len(owner_ids), batch):
        for owner_id in owner_ids[offset : offset + batch]:
            if store.get_participant(owner_id) is not None:
                continue
            try:
                recorded = store.participant_artifacts(owner_id)
            except (OSError, TypeError, ValueError) as exc:
                logger.warning(
                    "orphaned artifact ownership for %s cannot be read; retrying: %s",
                    owner_id,
                    exc,
                )
                continue
            try:
                failures = await workers.to_thread(
                    cleanup_orphan_recorded,
                    owner_id,
                    recorded,
                    label="gc.orphan_artifacts",
                )
            except Exception as exc:
                failures = (f"unexpected cleanup failure: {exc}",)
            if failures:
                logger.warning(
                    "orphaned artifact cleanup deferred for %s; retry on the next GC: %s",
                    owner_id,
                    "; ".join(failures),
                )
                continue
            store.delete_participant_artifacts(owner_id)
            await asyncio.sleep(0)

    try:
        candidates = await workers.to_thread(
            orphan_paths,
            retained_ids | exclude,
            label="gc.discover_orphan_participants",
        )
    except Exception as exc:
        logger.warning("participant root discovery deferred; retry on the next GC: %s", exc)
        return
    for offset in range(0, len(candidates), batch):
        candidates_batch = tuple(
            candidate
            for candidate in candidates[offset : offset + batch]
            if store.get_participant(candidate[1]) is None
        )
        if not candidates_batch:
            await asyncio.sleep(0)
            continue
        try:
            failures = await workers.to_thread(
                cleanup_orphan_paths,
                candidates_batch,
                label="gc.orphan_participants",
            )
        except Exception as exc:
            failures = (f"unexpected cleanup failure: {exc}",)
        if failures:
            logger.warning(
                "orphan participant cleanup deferred; retry on the next GC: %s",
                "; ".join(failures),
            )
        await asyncio.sleep(0)


async def _sweep_bus(store: Store, cutoff: float, batch: int, refused_cap: int) -> int:
    """Delete old bus rows, then trim ``send.refused`` to the cap.

    ``send.refused`` events are the only record of a refused send
    (``_refuse_send`` deliberately writes no job row), so they are exempt
    from the age TTL and capped by row count instead. The newest uncleared
    transcript-identity-loss row for each live participant is also retained,
    because watcher restart replay depends on it; superseded rows age normally.
    """
    total = 0
    protected = await _active_identity_loss_audit_ids(store, batch)
    # Age-based deletion: everything old except send.refused; select ids first (no .limit()).
    after_id = 0
    while True:
        sub = (
            select(bus.c.id)
            .where(bus.c.ts < cutoff)
            .where(bus.c.kind != BUS_KIND_SEND_REFUSED)
            .where(bus.c.id > after_id)
            .order_by(bus.c.id)
            .limit(batch)
        )
        scanned = [r[0] for r in store.conn.execute(sub).fetchall()]
        if not scanned:
            break
        after_id = scanned[-1]
        ids = [row_id for row_id in scanned if row_id not in protected]
        if ids:
            result = store.conn.execute(delete(bus).where(bus.c.id.in_(ids)))
            total += result.rowcount
        await asyncio.sleep(0)
        if len(scanned) < batch:
            break

    # Cap-based trimming of send.refused: keep the newest refused_cap rows.
    count_stmt = select(bus.c.id).where(bus.c.kind == BUS_KIND_SEND_REFUSED)
    refused_ids = [r[0] for r in store.conn.execute(count_stmt).fetchall()]
    if len(refused_ids) > refused_cap:
        # IDs are autoincrement, so higher id = newer. Keep the newest.
        to_delete = sorted(refused_ids)[:-refused_cap] if refused_cap > 0 else refused_ids
        if to_delete:
            # Batch the deletion to avoid a single huge statement.
            for i in range(0, len(to_delete), batch):
                chunk = to_delete[i : i + batch]
                result = store.conn.execute(delete(bus).where(bus.c.id.in_(chunk)))
                total += result.rowcount
                await asyncio.sleep(0)

    return total


async def _active_identity_loss_audit_ids(store: Store, batch: int) -> set[int]:
    """Newest uncleared loss event for each live participant, in batches.

    Quarantine is restart-replayed from the bus rather than stored on the
    participant row. Its active audit row therefore outlives the ordinary bus
    TTL. Superseded loss rows remain retention-bounded, and dead/orphaned rows
    are not protected because dead bindings are never quarantined.
    """
    kinds = tuple(TRANSCRIPT_AUDIT_KINDS)
    decided: set[str] = set()
    active: dict[str, int] = {}
    before_id: int | None = None
    while True:
        stmt = (
            select(bus.c.id, bus.c.to_id, bus.c.kind, bus.c.payload)
            .where(bus.c.to_id.is_not(None))
            .where(bus.c.kind.in_(kinds))
            .order_by(bus.c.id.desc())
            .limit(batch)
        )
        if before_id is not None:
            stmt = stmt.where(bus.c.id < before_id)
        rows = store.conn.execute(stmt).fetchall()
        if not rows:
            break
        for row_id, participant_id, kind, payload in rows:
            if participant_id in decided:
                continue
            try:
                decoded = json.loads(payload or "{}")
            except ValueError:
                decoded = {}
            clears = kind in {
                "agent.transcript",
                "operator.transcript_bind",
                "operator.transcript_unbind",
            } or (kind == "agent.transcript_receipt" and decoded.get("admission") == "accepted")
            if clears:
                decided.add(participant_id)
            elif (
                kind == "agent.observation_error"
                and decoded.get("code") == TRANSCRIPT_IDENTITY_LOST_CODE
            ):
                active[participant_id] = row_id
                decided.add(participant_id)
        before_id = rows[-1][0]
        await asyncio.sleep(0)
        if len(rows) < batch:
            break

    live: set[str] = set()
    participant_ids = list(active)
    lookup_batch = min(batch, 500)
    for offset in range(0, len(participant_ids), lookup_batch):
        chunk = participant_ids[offset : offset + lookup_batch]
        rows = store.conn.execute(
            select(participants.c.id)
            .where(participants.c.id.in_(chunk))
            .where(participants.c.status != "dead")
        ).fetchall()
        live.update(row[0] for row in rows)
        await asyncio.sleep(0)
    return {row_id for participant_id, row_id in active.items() if participant_id in live}


async def _sweep_scratchpad(store: Store, batch: int) -> int:
    """Delete physically expired global scratchpad rows in bounded writes."""
    total = 0
    cutoff = now()
    while True:
        deleted = store.scratchpad_delete_expired(timestamp=cutoff, limit=batch)
        total += deleted
        await asyncio.sleep(0)
        if deleted < batch:
            break
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
