"""Daemon composition of the :class:`ControlGates` injection seams.

Send stays open; steer/queue/settings/interrupt need the direct parent or ``"cli"``, like
kill. Gates read collaborators at call time, so build order never matters.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import NoReturn

from theater import protocol
from theater.daemon.controls.gates import ControlGates
from theater.daemon.controls.service import ACTION_SEND
from theater.daemon.operations import OperationNotFound
from theater.models import (
    BadRequest,
    Busy,
    ControlOwnerKind,
    JobState,
    NotYourChild,
    PublicOperationState,
    Status,
    TheaterError,
)

logger = logging.getLogger("theater.daemon.controls")

#: Refusal error for a control the caller does not own.
_NOT_YOUR_CHILD_CONTROLS = (
    "refusing this control for {target!r}: its parent is {parent!r}, not you "
    "({caller!r}); steering, queued followups, settings updates, and "
    "interruption belong to the direct parent or the local operator"
)


def build_control_gates(daemon) -> ControlGates:
    """Wire every physical/policy fact the control service needs."""
    from theater.daemon.presence import access

    return ControlGates(
        authorize=_authorize(daemon),
        require_absent=_require_absent(daemon),
        check_absent=lambda participant_id: access.check_absent(daemon, participant_id),
        send_preflight=_send_preflight(daemon),
        legacy_copy_mode_check=_legacy_copy_mode_check(daemon),
        legacy_busy_check=_legacy_busy_check(daemon),
        check_prompt=check_prompt,
        check_settings=check_settings,
        cwd_for=_cwd_for(daemon),
        legacy_deliver=_legacy_deliver(daemon),
        provider_health=lambda provider_id, generation: (
            daemon.terminal_service.connections.health(provider_id)
            if daemon.terminal_service.connections.is_current(provider_id, generation)
            else "offline"
        ),
        provider_dispatch=_provider_dispatch(daemon),
        record_native_snapshot=daemon.runtime_manager.record_snapshot,
        project_send_preflight=_project_send_preflight(daemon),
        settings_allowlists=lambda harness: (
            tuple(daemon.config.models_for(harness)),
            tuple(daemon.config.reasoning_for(harness)),
        ),
        terminal_interrupt_plan=_terminal_interrupt_plan(daemon),
    )


def _terminal_interrupt_plan(daemon):
    def plan(participant_id):
        from theater.harness import HARNESSES, normalize

        participant = daemon.registry.get(participant_id)
        harness = HARNESSES.get(normalize(participant.harness))
        return None if harness is None else harness.controls.interrupt

    return plan


def _project_send_preflight(daemon):
    def project_send_preflight(participant) -> tuple[str | None, str | None]:
        """Return the exact transcript refusal without creating refusal telemetry."""
        from theater.daemon.rpc import sending as sending_mod

        failure = sending_mod._transcript_send_failure(daemon, participant)
        if failure is None:
            return None, None
        exc, reason = failure
        return reason, str(exc)

    return project_send_preflight


def _provider_dispatch(daemon):
    async def provider_dispatch(provider_id, generation, method, params):
        operation_id = params.get("operation_id")
        if not isinstance(operation_id, str):
            raise TypeError("provider mutation requires an operation id")
        try:
            daemon.operation_service.get(operation_id)
        except OperationNotFound:
            return await daemon.terminal_service.connections.request(
                provider_id, generation, method, params
            )
        outcome = await daemon.terminal_service.dispatch_operation(
            provider_id, generation, method, params
        )
        if isinstance(outcome.result, Mapping):
            return outcome.result
        return {
            "operation_id": operation_id,
            "provider_generation": generation,
            "terminal_id": params.get("terminal_id"),
            "terminal_incarnation": params.get("terminal_incarnation"),
            "delivery": (
                "unknown" if outcome.state == PublicOperationState.UNCERTAIN.value else "rejected"
            ),
            "error": outcome.error,
        }

    return provider_dispatch


def _require_absent(daemon):
    from theater.daemon.presence import access

    async def require_absent(participant_id: str) -> None:
        """Focus protection; the composed provider is resolved per call."""
        await access.require_absent(daemon, participant_id)

    return require_absent


def _authorize(daemon):
    def authorize(participant_id: str, caller_id: str, action: str) -> None:
        if action == ACTION_SEND:
            # Ordinary send retains its current permission: any participant
            # may prompt any other, exactly like the existing send RPC.
            return
        if caller_id == "cli":
            return
        target = daemon.registry.get(participant_id)
        if caller_id == target.id:
            raise NotYourChild(
                f"refusing this control for {participant_id!r}: a participant "
                "does not steer, queue, retune, or interrupt itself through "
                "the control service"
            )
        owner_kind = target.control_owner_kind or (
            ControlOwnerKind.PARTICIPANT
            if target.parent_id is not None
            else ControlOwnerKind.LOCAL_OPERATOR
        )
        owner_id = target.control_owner_id or target.parent_id
        if owner_kind is not ControlOwnerKind.PARTICIPANT or owner_id != caller_id:
            raise NotYourChild(
                _NOT_YOUR_CHILD_CONTROLS.format(
                    target=participant_id, parent=owner_id, caller=caller_id
                )
            )

    return authorize


def _send_preflight(daemon):
    async def send_preflight(participant_id: str) -> None:
        """Shared transcript delivery checks; providers own terminal evidence."""
        from theater.daemon.rpc import sending as sending_mod

        def refuse(exc: TheaterError, *, reason: str) -> NoReturn:
            exc.refusal_reason = reason
            raise exc

        target = daemon.registry.get(participant_id)
        sending_mod._check_transcript_send_preflight(daemon, target, refuse)

    return send_preflight


def _legacy_copy_mode_check(daemon):
    async def legacy_copy_mode_check(participant_id: str) -> None:
        """Historical legacy rows cannot regain a physical route."""
        from theater.models import NotAddressable

        raise NotAddressable(
            f"participant {participant_id!r} has no current terminal-provider route"
        )

    return legacy_copy_mode_check


def _legacy_busy_check(daemon):
    async def legacy_busy_check(participant_id: str) -> None:
        """Legacy busy semantics: working status plus the send-claim window.

        Only delivered jobs (or plain send claims) count: a queued followup's job is RUNNING before
        dispatch, so counting it would busy-refuse the queue head and nothing would ever dispatch.
        """
        from theater.constants.daemon import SEND_SUPERSEDED_ERROR_CODE
        from theater.daemon.rpc import sending as sending_mod

        target = daemon.registry.get(participant_id)
        if target.status is Status.WORKING:
            raise Busy(f"participant {participant_id!r} is working; not delivering now")
        stale = sending_mod.now() - sending_mod._send_claim_ttl()
        running_prompt_jobs = [
            job for job in daemon.store.active_running_jobs_for_target(participant_id) if job.prompt
        ]
        for job in (item for item in running_prompt_jobs if item.created_at <= stale):
            daemon.jobs.finish(
                job.handle,
                state=JobState.CRASHED,
                result=(
                    f"Send to participant {participant_id!r} was superseded after its "
                    "delivery claim expired; await the newer send handle instead."
                ),
                error_code=SEND_SUPERSEDED_ERROR_CODE,
            )
        if any(job.created_at > stale for job in running_prompt_jobs):
            raise Busy(f"participant {participant_id!r} has a running send job")

    return legacy_busy_check


def check_prompt(prompt) -> None:
    """Prompt bounds reused by ordinary send and queued followups alike."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise BadRequest("prompt must be a non-empty string")
    if len(prompt.encode("utf-8", errors="replace")) > protocol.MAX_MESSAGE_BYTES:
        raise BadRequest(f"prompt exceeds the {protocol.MAX_MESSAGE_BYTES}-byte message ceiling")


def check_settings(model, reasoning_effort) -> None:
    """Shape validation for settings updates.

    The signature lacks the target, so per-harness allowlists are revalidated by the routing
    RPC. Approval and sandbox have no gate anywhere: the service cannot set them.
    """
    for name, value in (("model", model), ("reasoning_effort", reasoning_effort)):
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise BadRequest(f"{name} must be a non-empty string or null")


def _cwd_for(daemon):
    def cwd_for(participant_id: str) -> str | None:
        participant = daemon.store.get_participant(participant_id)
        return participant.cwd if participant is not None else None

    return cwd_for


def _legacy_deliver(daemon):
    async def legacy_deliver(participant_id: str, prompt: str) -> None:
        """Refuse historical rows rather than replaying through an inferred route."""
        del prompt
        from theater.models import NotAddressable

        raise NotAddressable(
            f"participant {participant_id!r} has no current terminal-provider route"
        )

    return legacy_deliver
