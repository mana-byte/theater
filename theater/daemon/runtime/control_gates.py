"""Daemon composition of the :class:`ControlGates` injection seams.

The control service owns authorization ordering, idle checks, job
correlation, and delivery recovery — but none of the physical facts. Every
fact arrives through a gate built here from the daemon's own existing policy:
pane identity, human presence, approval-modal detection, legacy busy
semantics, prompt bounds, working directories, and legacy tmux delivery.

Ordinary send keeps its current open permission; the added controls (steer,
queue, settings, interrupt) require the direct parent or the local operator
(``"cli"``), the same authorization family the kill path already enforces.
The gates close over the daemon and read its collaborators at call time, so
construction order in the composition root never matters.
"""

from __future__ import annotations

import logging
from typing import NoReturn

from theater import protocol
from theater.daemon.controls.gates import ControlGates
from theater.daemon.controls.service import ACTION_SEND
from theater.models import (
    BadRequest,
    Busy,
    JobState,
    NotYourChild,
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
    )


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
        if target.parent_id != caller_id:
            raise NotYourChild(
                _NOT_YOUR_CHILD_CONTROLS.format(
                    target=participant_id, parent=target.parent_id, caller=caller_id
                )
            )

    return authorize


def _send_preflight(daemon):
    async def send_preflight(participant_id: str) -> None:
        """Shared pane, approval, and transcript delivery checks; no copy mode."""
        from theater.daemon.rpc import sending as sending_mod

        def refuse(exc: TheaterError, *, reason: str) -> NoReturn:
            exc.refusal_reason = reason
            raise exc

        target = daemon.registry.get(participant_id)
        if not target.addressable or not target.tmux_pane:
            from theater.models import NotAddressable

            raise NotAddressable(f"participant {participant_id!r} has no pane to deliver to")
        await sending_mod._check_pane_identity(daemon, target, refuse)
        await sending_mod._check_approval_modal(daemon, target, refuse)
        sending_mod._check_transcript_send_preflight(daemon, target, refuse)

    return send_preflight


def _legacy_copy_mode_check(daemon):
    async def legacy_copy_mode_check(participant_id: str) -> None:
        """Copy mode blocks legacy key injection only; native delivery skips it."""
        from theater.daemon.rpc import sending as sending_mod

        target = daemon.registry.get(participant_id)
        if not target.tmux_pane:
            return
        refusal = await sending_mod.copy_mode_refusal(target.tmux_pane)
        if refusal is not None:
            raise refusal

    return legacy_copy_mode_check


def _legacy_busy_check(daemon):
    async def legacy_busy_check(participant_id: str) -> None:
        """Legacy busy semantics: working status plus the send-claim window.

        Mirrors the send RPC: an expired prompt claim is closed as superseded
        before a fresh reservation may proceed, and any unexpired active
        prompt job refuses with ``busy``. The active-job seam is what keeps
        this composable with the followup queue: a queued followup's job is
        created RUNNING before it dispatches, so counting every running job
        would busy-refuse the queue head against its own fresh prompt and no
        legacy followup would ever dispatch. Only jobs actually delivered to
        the target — or legacy claim jobs with no control operation at all,
        the ordinary send's — block and supersede by the old TTL window.
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

    The frozen gate signature carries only the values, not the target, so
    per-harness allowlists cannot be enforced here; the composition that
    routes settings RPCs (Wave 4) revalidates against the target harness's
    configured allowlists before calling the service. Approval and sandbox
    policy have no gate anywhere: the service has no field that could set
    them.
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
        """Legacy tmux text delivery; an exception means nothing was delivered."""
        from theater.tmux import client as tmux

        target = daemon.registry.get(participant_id)
        await tmux.deliver_text(target.tmux_pane, prompt)

    return legacy_deliver
