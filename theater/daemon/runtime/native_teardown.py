"""Verified native backend teardown and dead-participant reaping."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from theater.daemon.harness_runtime.errors import BackendIdentityMismatch
from theater.daemon.harness_runtime.exact_stop import (
    BackendAdoptionFailed,
    BackendTeardownFailed,
    persisted_backend_identity,
    stop_exact_backend,
)
from theater.daemon.spawning.frontend import close_frontend_runtime, is_frontend_binding
from theater.models import Status

if TYPE_CHECKING:
    from theater.daemon.server import Daemon

logger = logging.getLogger("theater.daemon.runtime")


async def teardown_participant_runtime(
    daemon: Daemon, participant_id: str, *, caller_id: str
) -> bool:
    """Explicit kill / confirmed exit: stop the verified backend; ``True`` allows pane retirement.

    ``False`` keeps the binding for retry (the backend may still use pane/worktree); a pid
    without strong start identity is not ours and never signalled.
    """
    binding = daemon.store.get_runtime_binding(participant_id)
    if binding is None:
        return True
    if is_frontend_binding(binding):
        await close_frontend_runtime(daemon, participant_id)
        daemon.store.delete_runtime_binding(participant_id)
        return True
    try:
        # Gate-free queue cancel: the pane is already gone here, so a presence refresh could only
        # refuse on kill wake churn or pass on stale facts. The backend stops below anyway.
        await daemon.controls.cancel_queued_followups(participant_id)
    except Exception:
        logger.exception(
            "queue cancellation for %s failed; backend teardown continues",
            participant_id,
        )
    identity = persisted_backend_identity(binding.backend_pid, binding.backend_started_at)
    if identity is None:
        logger.warning(
            "cannot terminate the backend of %s: no verified process identity was "
            "ever persisted, so no signal may be sent and no retirement may "
            "proceed; the binding is kept and any live process is left for "
            "manual inspection",
            participant_id,
        )
        return False
    try:
        await stop_exact_backend(
            daemon.runtime_manager,
            participant_id,
            backend_generation=binding.backend_generation,
            identity=identity,
            endpoint=binding.endpoint,
        )
    except BackendIdentityMismatch:
        # The persisted identity is the authority: a pid that no longer
        # verifies is a backend that exited, positively — safe.
        pass
    except BackendAdoptionFailed:
        logger.exception(
            "could not adopt the persisted backend of %s for teardown; "
            "the binding is kept and retirement must not proceed",
            participant_id,
        )
        return False
    except BackendTeardownFailed:
        logger.exception(
            "backend teardown for %s failed; the binding is kept and "
            "retirement must not proceed — a backend may still be running",
            participant_id,
        )
        return False
    _unregister_live(daemon, participant_id)
    daemon.store.delete_runtime_binding(participant_id)
    return True


def _unregister_live(daemon: Daemon, participant_id: str) -> None:
    """Return a retired participant to durable-only observation."""
    hub = getattr(daemon.observer, "live", None)
    if hub is not None:
        hub.unregister(participant_id)


async def sweep_dead_participant_backends(daemon: Daemon) -> None:
    """Reaper pass: a dead participant owns no live backend.

    Catches endings outside the kill flow and retries failed teardowns (in-flight kills are
    left to it); releases workspace usage once a stop is proved, files stay until cleanup.
    """
    for binding in daemon.store.runtime_bindings_for_recovery():
        if binding.participant_id in daemon._explicit_kills:
            continue
        participant = daemon.store.get_participant(binding.participant_id)
        if participant is not None and participant.status is not Status.DEAD:
            continue
        stopped = await teardown_participant_runtime(
            daemon, binding.participant_id, caller_id="cli"
        )
        if not stopped or participant is None:
            continue
        daemon.spawner.release_workspace_usage(participant, reason="participant_exit")
