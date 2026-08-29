"""Shared tmux inventory reconciliation for daemon startup and reaping."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from theater.constants.daemon import (
    BUS_KIND_TMUX_SERVER_RESTART,
    TMUX_RESTART_JOB_ERROR_CODE,
    TMUX_SERVER_IDENTITY_META_KEY,
    TMUX_SERVER_RESTART_AFFECTED_IDS_LIMIT,
)
from theater.daemon.jobs import JobState
from theater.models import Participant, new_id, now
from theater.tmux import client as tmux

logger = logging.getLogger("theater.daemon")


@dataclass(frozen=True, slots=True)
class TmuxReconciliation:
    """The result of one inventory decision, or an inconclusive observation."""

    pane_ids: frozenset[str] | None
    server_identity: str | None = None
    reset_incident: str | None = None

    @property
    def conclusive(self) -> bool:
        return self.pane_ids is not None


@dataclass(frozen=True, slots=True)
class TmuxRestart:
    incident: str
    terminated_at: float
    affected: tuple[Participant, ...]
    newly_owned: tuple[Participant, ...]


async def reconcile_tmux_inventory(daemon, *, context: str) -> TmuxReconciliation:
    """Apply one complete server-identity and pane-inventory decision."""
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

    previous_identity = daemon.store.get_meta(TMUX_SERVER_IDENTITY_META_KEY)
    if previous_identity is None:
        daemon.store.set_meta(TMUX_SERVER_IDENTITY_META_KEY, inventory.server_identity)
        daemon.store.stamp_live_tmux_server_identity(inventory.server_identity)
        return TmuxReconciliation(
            pane_ids=inventory.pane_ids,
            server_identity=inventory.server_identity,
        )

    if inventory.server_identity != previous_identity:
        restart = _classify_tmux_restart(
            daemon.registry.list(),
            previous_identity=previous_identity,
            new_panes=inventory.pane_ids,
        )
        daemon.store.record_tmux_server_restart(
            server_identity=inventory.server_identity,
            affected_ids=[participant.id for participant in restart.affected],
            newly_owned_ids=[participant.id for participant in restart.newly_owned],
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
        daemon.store.bus_append(
            BUS_KIND_TMUX_SERVER_RESTART,
            payload={
                "incident": restart.incident,
                "affected_count": len(restart.affected),
                "affected_ids": [
                    participant.id
                    for participant in restart.affected[:TMUX_SERVER_RESTART_AFFECTED_IDS_LIMIT]
                ],
            },
        )
        return TmuxReconciliation(
            pane_ids=inventory.pane_ids,
            server_identity=inventory.server_identity,
            reset_incident=restart.incident,
        )

    daemon.store.stamp_live_tmux_server_identity(inventory.server_identity)
    await _retire_missing_panes(daemon, inventory.pane_ids, context=context)
    return TmuxReconciliation(
        pane_ids=inventory.pane_ids,
        server_identity=inventory.server_identity,
    )


def _classify_tmux_restart(
    participants: list[Participant],
    *,
    previous_identity: str,
    new_panes: frozenset[str],
) -> TmuxRestart:
    affected: list[Participant] = []
    newly_owned: list[Participant] = []
    for participant in participants:
        if not participant.tmux_pane:
            continue
        if participant.tmux_server_identity == previous_identity:
            affected.append(participant)
        elif participant.tmux_server_identity is None:
            if participant.tmux_pane in new_panes:
                newly_owned.append(participant)
            else:
                affected.append(participant)
    return TmuxRestart(
        incident=new_id(),
        terminated_at=now(),
        affected=tuple(affected),
        newly_owned=tuple(newly_owned),
    )


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
