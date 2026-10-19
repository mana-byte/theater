"""Verified participant termination and cleanup."""

from __future__ import annotations

from theater import timing
from theater.constants.daemon import BUS_KIND_PARTICIPANT_KILL_REQUESTED
from theater.constants.refusals import (
    NEXT_ASK_OPERATOR,
    NEXT_FORGET,
    NEXT_NOT_YOUR_CHILD,
    NO_TERMINAL_PRESENCE_REASON,
)
from theater.daemon.forgetting import forget_participant, forget_refusal
from theater.daemon.operations.termination import terminate_with_operation
from theater.daemon.presence import access as presence_access
from theater.daemon.worktrees.retirement import cleanup_killed_workspace
from theater.models import (
    ControlOwnerKind,
    HumanPresent,
    JobState,
    NoSelfKill,
    NotYourChild,
    Participant,
    Status,
    TheaterError,
)
from theater.observability.catalog import LIFECYCLE_STAGE


async def _require_verified_backend_stop(daemon, pid: str, caller_id: str) -> None:
    """Terminate the verified native backend of ``pid`` before pane/worktree cleanup.

    An unproven stop raises, leaving worktree and binding for the reaper's retries.
    """
    from theater.daemon.spawning.frontend import close_frontend_runtime, is_frontend_binding

    del caller_id
    binding = daemon.store.get_runtime_binding(pid)
    if binding is None:
        return
    if is_frontend_binding(binding):
        try:
            await close_frontend_runtime(daemon, pid)
        except Exception:
            stopped = False
        else:
            stopped = True
    else:
        stopped = await _stop_verified_detached_backend(daemon, pid, binding)
    if not stopped:
        raise TheaterError(
            f"kill of {pid!r}: the backend teardown could not be verified; "
            "the runtime binding and worktree are preserved for the reaper "
            "to retry — inspect the backend process before retrying"
        )


async def _stop_verified_detached_backend(daemon, pid: str, binding) -> bool:
    """Stop one exact detached backend without mutating queued control state."""
    from theater.daemon.harness_runtime.errors import BackendIdentityMismatch

    if binding.backend_pid is None or binding.backend_started_at is None:
        return False
    if daemon.runtime_manager.backend(pid) is None:
        try:
            await daemon.runtime_manager.adopt_backend(
                pid,
                backend_generation=binding.backend_generation,
                pid=binding.backend_pid,
                started_at=binding.backend_started_at,
                endpoint=binding.endpoint,
            )
        except BackendIdentityMismatch:
            hub = getattr(daemon.observer, "live", None)
            if hub is not None:
                hub.unregister(pid)
            return True
        except Exception:
            return False
    try:
        await daemon.runtime_manager.teardown(pid, backend_generation=binding.backend_generation)
    except Exception:
        return False
    hub = getattr(daemon.observer, "live", None)
    if hub is not None:
        hub.unregister(pid)
    return True


class _TerminationUncertain(TheaterError):
    code = "provider_unavailable"

    def __init__(self, participant_id: str, *, reason: str | None = None) -> None:
        self.details = {"possibly_executed": True, "participant_id": participant_id}
        super().__init__(
            f"termination of {participant_id!r} may have executed but exit was not "
            "verified; workspace usage remains held" + (f": {reason}" if reason else "")
        )


async def _terminate_provider_terminal(
    daemon, participant_id: str, caller_id: str, operation_id: str
) -> None:
    result = await daemon.controls.terminate_provider(
        participant_id,
        caller_id=caller_id,
        callback_operation_id=operation_id,
    )
    delivery = result.get("delivery")
    if delivery == "unknown" or (delivery == "accepted" and not result.get("exit_confirmed")):
        raise _TerminationUncertain(participant_id)
    if delivery != "accepted" or result.get("exit_confirmed") is not True:
        error_value = result.get("error")
        detail = (
            error_value.get("message")
            if isinstance(error_value, dict)
            else "provider rejected termination"
        )
        raise TheaterError(str(detail))


def _authorize_termination(target: Participant, caller_id: str) -> None:
    if caller_id == "cli":
        return
    if target.id == caller_id:
        raise NoSelfKill(f"refusing to kill {target.id!r}: that is you, not your child")
    owner_kind = target.control_owner_kind or (
        ControlOwnerKind.PARTICIPANT
        if target.parent_id is not None
        else ControlOwnerKind.LOCAL_OPERATOR
    )
    owner_id = target.control_owner_id or target.parent_id
    if owner_kind is not ControlOwnerKind.PARTICIPANT or owner_id != caller_id:
        raise NotYourChild(
            f"refusing to kill {target.id!r}: its current control owner is "
            f"{owner_id or owner_kind.value!r}, not you ({caller_id!r})",
            details={"next": NEXT_NOT_YOUR_CHILD.format(owner=owner_id or owner_kind.value)},
        )


async def _require_absent_or_forget_hint(daemon, target: Participant, caller_id: str) -> None:
    """An unbound target never gains presence evidence: point at forget, not at waiting."""
    try:
        await presence_access.require_absent(daemon, target.id)
    except HumanPresent as exc:
        if NO_TERMINAL_PRESENCE_REASON not in str(exc):
            raise
        allowed = forget_refusal(daemon, target, caller_id) is None
        exc.details = {**(exc.details or {}), "next": NEXT_FORGET if allowed else NEXT_ASK_OPERATOR}
        raise


async def terminate_participant(
    daemon,
    pid: str,
    *,
    caller_id: str,
    operation_id: str | None = None,
    forget: bool = False,
) -> dict:
    """Verify exit, retire participant state, then clean its unique workspace."""
    target = daemon.registry.resolve(pid)
    pid = target.id

    if forget:
        return await forget_participant(daemon, target, caller_id=caller_id)
    _authorize_termination(target, caller_id)
    if target.status is Status.DEAD:
        return {"id": pid, "killed": False, "reason": "already_dead"}
    if daemon.handoff.cancel_unlaunched(pid):
        # A deferred task has no terminal, pane or process: there is nothing to verify or kill.
        return {"id": pid, "killed": True, "reason": "never_launched"}
    if operation_id is None:
        return await terminate_with_operation(
            daemon,
            pid,
            caller_id=caller_id,
            execute=lambda durable_id: terminate_participant(
                daemon, pid, caller_id=caller_id, operation_id=durable_id
            ),
        )

    terminal_binding = daemon.store.terminal_bindings.get(pid)
    daemon._explicit_kills.add(pid)
    try:
        if terminal_binding is not None:
            # Admission checks presence under the target's control lock; the provider
            # re-verifies it immediately before killing the terminal.
            await _terminate_provider_terminal(daemon, pid, caller_id, operation_id)
        else:
            with timing.span(
                LIFECYCLE_STAGE,
                action="kill",
                stage="presence",
                id=pid,
                operation_id=operation_id,
            ):
                await _require_absent_or_forget_hint(daemon, target, caller_id)
        # Verify every execution surface before committing participant, job, or usage state.
        try:
            with timing.span(
                LIFECYCLE_STAGE,
                action="kill",
                stage="backend_exit",
                id=pid,
                operation_id=operation_id,
            ):
                await _require_verified_backend_stop(daemon, pid, caller_id)
        except Exception as exc:
            if operation_id is not None:
                raise _TerminationUncertain(pid, reason=str(exc)) from exc
            raise
        await daemon.controls.cancel_queued_followups(pid)
        daemon.store.bus_append(
            BUS_KIND_PARTICIPANT_KILL_REQUESTED,
            from_id=caller_id,
            to_id=pid,
        )
        with timing.span(
            LIFECYCLE_STAGE,
            action="kill",
            stage="retire",
            id=pid,
            operation_id=operation_id,
        ):
            # Job completion hashes files before teardown releases workspace usage.
            pinned = await daemon.jobs.prepare_pins(pid)
            try:
                for job in daemon.store.running_jobs_for_target(pid):
                    daemon.jobs.finish(job.handle, state=JobState.KILLED, error_code="killed")
            finally:
                daemon.jobs.release_pins(pinned)
            # Keep the durable binding through every awaited verification/cancellation boundary.
            # From here teardown performs only synchronous registry and usage transitions.
            daemon.store.delete_runtime_binding(pid)
            await daemon.spawner.teardown(target)
    finally:
        daemon._explicit_kills.discard(pid)

    result = {"id": pid, "killed": True}
    with timing.span(
        LIFECYCLE_STAGE,
        action="kill",
        stage="workspace_cleanup",
        id=pid,
        operation_id=operation_id,
    ):
        cleanup = await cleanup_killed_workspace(daemon, target)
    if cleanup is not None:
        result["workspace_cleanup"] = cleanup
    return result
