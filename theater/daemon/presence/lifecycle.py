"""Apply exact provider terminal-exit evidence to daemon lifecycle policy."""

from __future__ import annotations

import logging

from theater.daemon.jobs import JobState
from theater.daemon.presence.provider import ProviderExitEvidence
from theater.models import Status

logger = logging.getLogger("theater.daemon.presence")


async def retire_authoritative_exit(daemon, evidence: ProviderExitEvidence) -> bool:
    """Retire only while the observed terminal still owns the live binding."""
    binding = daemon.store.terminal_bindings.get(evidence.participant_id)
    service = getattr(daemon, "terminal_service", None)
    if (
        binding is None
        or service is None
        or not evidence.matches(binding)
        or not service.connections.is_current(evidence.provider_id, evidence.provider_generation)
    ):
        return False
    participant = daemon.store.get_participant(evidence.participant_id)
    if participant is None or participant.status is Status.DEAD:
        return True
    if participant.id in getattr(daemon, "_explicit_kills", ()):
        return False  # The explicit kill owns job settlement and workspace cleanup.

    logger.info(
        "terminal exited id=%s harness=%s provider=%s generation=%s terminal=%s reason=%s",
        participant.id,
        participant.harness,
        evidence.provider_id,
        evidence.provider_generation,
        evidence.terminal_id,
        evidence.lifecycle.get("reason", "terminal_exited"),
    )
    daemon.registry.mark_dead(participant.id)
    finish_failed = False
    for job in daemon.store.running_jobs_for_target(participant.id):
        try:
            daemon.jobs.finish(job.handle, state=JobState.CRASHED, error_code="crashed")
        except Exception:
            finish_failed = True
            logger.exception("job finish failed after provider exit for %s", participant.id)
    if finish_failed:
        return True

    try:
        from theater.daemon.runtime.recovery import teardown_participant_runtime

        stopped = await teardown_participant_runtime(daemon, participant.id, caller_id="cli")
    except Exception:
        logger.exception(
            "runtime teardown failed after provider exit for %s; usage remains held",
            participant.id,
        )
        return True
    if not stopped:
        logger.warning(
            "runtime teardown after provider exit was unverified for %s; usage remains held",
            participant.id,
        )
        return True
    try:
        daemon.spawner.release_workspace_usage(participant, reason="participant_exit")
    except Exception:
        logger.exception(
            "workspace usage release failed after provider exit for %s",
            participant.id,
        )
    return True


__all__ = ["retire_authoritative_exit"]
