"""Shared tmux inventory reconciliation for daemon startup and reaping."""

from __future__ import annotations

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
        return await reconcile_tmux_inventory_locked(daemon, context=context)


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
    if retire_missing:
        await _retire_missing_panes(daemon, inventory.pane_ids, context=context)
    return TmuxReconciliation(
        pane_ids=inventory.pane_ids,
        server_identity=inventory.server_identity,
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


async def _retire_missing_panes(daemon, alive_panes: frozenset[str], *, context: str) -> None:
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
        try:
            await daemon.spawner.retire(participant, delete_branch=False)
        except Exception:
            logger.exception("retire failed for %s; marking dead anyway", participant.id)
        daemon.registry.mark_dead(participant.id)
        for job in daemon.store.running_jobs_for_target(participant.id):
            daemon.jobs.finish(job.handle, state=JobState.CRASHED, error_code="crashed")
