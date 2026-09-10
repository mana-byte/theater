"""Shared tmux inventory reconciliation for daemon startup and reaping."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from theater.constants.daemon import TMUX_RESTART_JOB_ERROR_CODE, TMUX_SERVER_IDENTITY_META_KEY
from theater.daemon.jobs import JobState
from theater.models import Participant, new_id, now
from theater.tmux import client as tmux

logger = logging.getLogger("theater.daemon")


@dataclass(frozen=True, slots=True)
class TmuxReconciliation:
    """The result of one inventory decision, or an inconclusive observation."""

    pane_ids: frozenset[str] | None
    server_identity: str | None = None
    retirements: tuple[Participant, ...] = ()

    def identity_for_pane(self, pane_id: str) -> str | None:
        if self.pane_ids is None or self.server_identity is None:
            return None
        return self.server_identity if pane_id in self.pane_ids else None


@dataclass(frozen=True, slots=True)
class TmuxRestart:
    incident: str
    terminated_at: float
    affected: tuple[Participant, ...]


async def reconcile_tmux_inventory(daemon, *, context: str) -> TmuxReconciliation:
    """Apply one complete server-identity and pane-inventory decision."""
    async with daemon._tmux_reconcile_lock:
        reconciliation = await reconcile_tmux_inventory_locked(daemon, context=context)
    await retire_reconciled_participants(daemon, reconciliation, context=context)
    return reconciliation


async def reconcile_tmux_inventory_locked(
    daemon,
    *,
    context: str,
    retire_missing: bool = True,
) -> TmuxReconciliation:
    """Apply one inventory decision while the daemon reconciliation lock is held."""
    if not tmux.available():
        logger.info("%s: tmux unavailable; skipping reconciliation", context)
        return TmuxReconciliation(pane_ids=None)
    try:
        inventory = await tmux.observe_inventory()
    except Exception as exc:
        logger.warning("%s: could not observe tmux inventory: %s", context, exc)
        return TmuxReconciliation(pane_ids=None)
    if inventory is None:
        tracked = sum(1 for p in daemon.registry.list() if p.tmux_pane)
        logger.warning("%s: empty pane inventory with %d tracked panes; skipping", context, tracked)
        return TmuxReconciliation(pane_ids=None)

    participants = daemon.registry.list()
    previous_identity = daemon.store.get_meta(TMUX_SERVER_IDENTITY_META_KEY)
    stamped_ids = _identity_less_participant_ids(participants, inventory.pane_ids)
    if previous_identity is None:
        daemon.store.set_meta(TMUX_SERVER_IDENTITY_META_KEY, inventory.server_identity)
        daemon.store.stamp_live_tmux_server_identity(
            inventory.server_identity,
            participant_ids=stamped_ids,
        )
        return TmuxReconciliation(
            pane_ids=inventory.pane_ids,
            server_identity=inventory.server_identity,
        )

    if inventory.server_identity != previous_identity:
        restart = _classify_tmux_restart(
            participants,
            previous_identity=previous_identity,
        )
        daemon.store.record_tmux_server_restart(
            server_identity=inventory.server_identity,
            affected_ids=[participant.id for participant in restart.affected],
            newly_owned_ids=[],
            incident=restart.incident,
            terminated_at=restart.terminated_at,
        )
        daemon.registry.finalize_tmux_restarted(list(restart.affected))
        for participant in restart.affected:
            for job in daemon.store.running_jobs_for_target(participant.id):
                daemon.jobs.finish(
                    job.handle,
                    state=JobState.CRASHED,
                    error_code=TMUX_RESTART_JOB_ERROR_CODE,
                )
        return TmuxReconciliation(
            pane_ids=inventory.pane_ids,
            server_identity=inventory.server_identity,
        )

    daemon.store.stamp_live_tmux_server_identity(
        inventory.server_identity,
        participant_ids=stamped_ids,
    )
    retirements = (
        _terminalize_missing_panes(daemon, inventory.pane_ids, context=context)
        if retire_missing
        else ()
    )
    return TmuxReconciliation(
        pane_ids=inventory.pane_ids,
        server_identity=inventory.server_identity,
        retirements=retirements,
    )


def _classify_tmux_restart(
    participants: list[Participant],
    *,
    previous_identity: str,
) -> TmuxRestart:
    affected = tuple(
        participant
        for participant in participants
        if participant.tmux_pane and participant.tmux_server_identity in (None, previous_identity)
    )
    return TmuxRestart(
        incident=new_id(),
        terminated_at=now(),
        affected=affected,
    )


def _identity_less_participant_ids(
    participants: list[Participant],
    pane_ids: frozenset[str],
) -> list[str]:
    return [
        participant.id
        for participant in participants
        if participant.tmux_server_identity is None and participant.tmux_pane in pane_ids
    ]


def _terminalize_missing_panes(
    daemon, alive_panes: frozenset[str], *, context: str
) -> tuple[Participant, ...]:
    """Mark vanished participants dead while the reconciliation lock is held.

    Worktree reads and removal happen later, outside the global tmux lock. The
    dead marker prevents another reconciliation or explicit kill from claiming
    the same participant while that cleanup is in flight.
    """
    retirements: list[Participant] = []
    for participant in daemon.registry.list():
        if not participant.tmux_pane or participant.tmux_pane in alive_panes:
            continue
        if participant.id in daemon._explicit_kills:
            continue
        logger.info(
            "%s: participant %s lost pane %s",
            context,
            participant.id,
            participant.tmux_pane,
        )
        daemon.registry.mark_dead(participant.id)
        retirements.append(participant)
    return tuple(retirements)


async def retire_reconciled_participants(
    daemon,
    reconciliation: TmuxReconciliation,
    *,
    context: str,
) -> None:
    """Finish jobs, then reclaim vanished worktrees without holding tmux lock.

    Cancellation waits for the bounded cleanup sequence before propagating, so
    daemon shutdown never leaves an untracked worker mutating a worktree.
    """
    if not reconciliation.retirements:
        return
    task = asyncio.create_task(
        _finish_and_retire(daemon, reconciliation.retirements, context=context)
    )
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def _finish_and_retire(
    daemon,
    participants: tuple[Participant, ...],
    *,
    context: str,
) -> None:
    for participant in participants:
        finish_failed = False
        for job in daemon.store.running_jobs_for_target(participant.id):
            try:
                daemon.jobs.finish(job.handle, state=JobState.CRASHED, error_code="crashed")
            except Exception:
                finish_failed = True
                logger.exception("job finish failed for vanished participant %s", participant.id)
        if finish_failed:
            logger.warning(
                "%s: preserving worktree for %s because job finalization failed",
                context,
                participant.id,
            )
            continue
        try:
            from theater.daemon.runtime.recovery import teardown_participant_runtime

            # Confirmed participant exit: the verified backend terminates
            # before pane/worktree cleanup, outside the global reconciliation
            # lock this pass already runs without.
            await teardown_participant_runtime(daemon, participant.id, caller_id="cli")
        except Exception:
            logger.exception(
                "runtime teardown failed for vanished participant %s; the "
                "binding is kept for the reaper to retry",
                participant.id,
            )
        try:
            await daemon.spawner.retire(participant, delete_branch=False)
        except Exception:
            logger.exception("retire failed for %s; participant remains dead", participant.id)
