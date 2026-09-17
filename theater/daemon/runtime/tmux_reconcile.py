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
    provider_bound_ids = _provider_bound_participant_ids(daemon, participants)
    previous_identity = daemon.store.get_meta(TMUX_SERVER_IDENTITY_META_KEY)
    stamped_ids = _identity_less_participant_ids(
        participants,
        inventory.pane_ids,
        excluded_ids=provider_bound_ids,
    )
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
            excluded_ids=provider_bound_ids,
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
    excluded_ids: frozenset[str] = frozenset(),
) -> TmuxRestart:
    affected = tuple(
        participant
        for participant in participants
        if participant.id not in excluded_ids
        and participant.tmux_pane
        and participant.tmux_server_identity in (None, previous_identity)
    )
    return TmuxRestart(
        incident=new_id(),
        terminated_at=now(),
        affected=affected,
    )


def _identity_less_participant_ids(
    participants: list[Participant],
    pane_ids: frozenset[str],
    *,
    excluded_ids: frozenset[str] = frozenset(),
) -> list[str]:
    return [
        participant.id
        for participant in participants
        if participant.id not in excluded_ids
        and participant.tmux_server_identity is None
        and participant.tmux_pane in pane_ids
    ]


def _provider_bound_participant_ids(daemon, participants: list[Participant]) -> frozenset[str]:
    """Exclude provider-owned terminals from legacy tmux lifecycle inference."""
    repository = getattr(daemon.store, "terminal_bindings", None)
    if repository is None:
        return frozenset()
    bound: set[str] = set()
    for participant in participants:
        try:
            if repository.get(participant.id) is not None:
                bound.add(participant.id)
        except Exception:
            logger.warning(
                "terminal binding lookup failed for %s; preserving it from tmux reconciliation",
                participant.id,
                exc_info=True,
            )
            bound.add(participant.id)
    return frozenset(bound)


def _terminalize_missing_panes(
    daemon, alive_panes: frozenset[str], *, context: str
) -> tuple[Participant, ...]:
    """Mark vanished participants dead while the reconciliation lock is held.

    Worktree reads and removal happen later, outside the global tmux lock. The
    dead marker prevents another reconciliation or explicit kill from claiming
    the same participant while that cleanup is in flight.
    """
    retirements: list[Participant] = []
    provider_bound_ids = _provider_bound_participant_ids(daemon, daemon.registry.list())
    for participant in daemon.registry.list():
        if participant.id in provider_bound_ids:
            continue
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
    """Finish jobs and release usage without holding the tmux lock.

    Cancellation waits for backend teardown before propagating. Workspaces are
    retained for explicit cleanup after participant exit.
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
            # before workspace usage is released, outside the global lock.
            stopped = await teardown_participant_runtime(daemon, participant.id, caller_id="cli")
        except Exception:
            logger.exception(
                "runtime teardown failed for vanished participant %s; the "
                "binding is kept for the reaper to retry",
                participant.id,
            )
            stopped = False
        if not stopped:
            # The backend's stop could not be proven: a backend may still be
            # running in the worktree, so usage remains held. The reaper
            # retries the teardown and releases usage once it is verified.
            logger.warning(
                "%s: backend teardown of %s could not be verified; the "
                "worktree and binding are preserved for the reaper to retry",
                context,
                participant.id,
            )
            continue
        try:
            daemon.spawner.release_workspace_usage(participant, reason="participant_exit")
        except Exception:
            logger.exception(
                "workspace usage release failed for %s; participant remains dead",
                participant.id,
            )
