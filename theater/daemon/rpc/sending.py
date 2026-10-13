"""Send RPC handler: preflight gates, pane identity, refusal, and delivery."""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import NoReturn

from theater.constants.daemon import BUS_KIND_SEND_REFUSED

# Definition re-exported by the methods facade; runtime reads the facade for legacy patches.
from theater.constants.daemon import SEND_CLAIM_TTL_SECONDS as SEND_CLAIM_TTL  # noqa: F401
from theater.constants.refusals import NEXT_HUMAN_PRESENT
from theater.daemon.rpc.params import (
    _require,
    _serialized_response_format,
)
from theater.daemon.rpc.router import method
from theater.daemon.rpc.skill_attachments import attach_skills
from theater.daemon.skill_cards import compose_prompt
from theater.harness import HARNESSES, normalize
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
)
from theater.models import (
    Busy,
    HumanPresent,
    JobState,
    TheaterError,
    Tier,
    TranscriptIdentityLost,
    TranscriptUntrusted,
    now,  # noqa: F401 — compatibility clock used by send-claim gates
)
from theater.provenance import is_trusted_provenance
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    transcript_identity_recovery_message,
)


def _send_claim_ttl() -> float:
    from theater.daemon import methods as _facade

    return _facade.SEND_CLAIM_TTL


logger = logging.getLogger(__name__)


def _transcript_identity_lost(daemon, pid: str) -> bool:
    checker = getattr(daemon.observer, "transcript_identity_lost", None)
    return bool(checker(pid)) if callable(checker) else False


def _refuse_send(
    daemon, exc: Exception, *, reason: str, caller_id: str, target_id: str
) -> NoReturn:
    """Record a send refused before delivery, then raise it.

    One bus kind with a ``reason`` so readers can subscribe to all refusals without the list.
    """
    daemon.store.bus_append(
        BUS_KIND_SEND_REFUSED,
        from_id=caller_id,
        to_id=target_id,
        payload={"reason": reason, "detail": str(exc)},
    )
    raise exc


async def _check_pane_identity(daemon, target, refuse: Callable[..., NoReturn]) -> None:
    """Retained compatibility seam; providers now verify terminal identity."""
    del daemon, target, refuse


async def _check_approval_modal(daemon, target, refuse: Callable[..., NoReturn]) -> None:
    """Retained compatibility seam; providers perform the terminal recheck."""
    del daemon, target, refuse


def _transcript_send_failure(daemon, target) -> tuple[TheaterError, str] | None:
    """Describe a transcript attribution refusal without recording it."""
    if _transcript_identity_lost(daemon, target.id):
        return (
            TranscriptIdentityLost(transcript_identity_recovery_message(target.id)),
            TRANSCRIPT_IDENTITY_LOST_CODE,
        )
    if is_trusted_provenance(target.session_correlation):
        return None
    ambiguity = getattr(daemon.observer, "transcript_correlation_ambiguous", None)
    if target.tier is not Tier.ADOPTED and not (callable(ambiguity) and ambiguity(target.id)):
        return None
    harness = HARNESSES.get(normalize(target.harness))
    if harness is None or not harness.observer.has_transcript:
        return None
    pid = target.id
    return (
        TranscriptUntrusted(
            f"participant {pid!r} has no trusted transcript identity: attribution is not yet "
            "operator/proven/exact. Screen-only status observation remains live, but "
            "Theater will not create a send job until attribution is trusted. Run "
            f"`theater candidates {pid}` to inspect candidates, then "
            f"`theater bind {pid} <candidate> --confirm-id {pid}` for the candidate "
            "you verified. If no candidates are listed yet, retry after the next "
            "observation poll before binding."
        ),
        "transcript_untrusted",
    )


def _check_transcript_send_preflight(daemon, target, refuse: Callable[..., NoReturn]) -> None:
    """Refuse untrusted adoption, observed ambiguity, or identity loss before job creation."""
    failure = _transcript_send_failure(daemon, target)
    if failure is not None:
        exc, reason = failure
        refuse(exc, reason=reason)


def _publish_send_event(
    daemon, *, caller_id: str, target_id: str, handle: str, prompt: str
) -> None:
    daemon.store.bus_append(
        "agent.send",
        from_id=caller_id,
        to_id=target_id,
        payload={"handle": handle, "prompt": prompt[:200]},
    )


def _publish_routed_send_event(
    daemon, *, caller_id: str, target_id: str, handle: str, prompt: str
) -> None:
    operations = daemon.store.control_operations_for_job(handle)
    # Historical legacy sends publish inside ControlService; routed sends publish here.
    if not any(
        operation.kind is ControlKind.SEND
        and operation.transport
        in {ControlTransport.NATIVE_RUNTIME, ControlTransport.PROVIDER_TERMINAL}
        and operation.delivery_result is not DeliveryResult.REJECTED
        for operation in operations
    ):
        return
    _publish_send_event(
        daemon,
        caller_id=caller_id,
        target_id=target_id,
        handle=handle,
        prompt=prompt,
    )


def _auto_result(daemon, job, queued: bool, caller_id: str, target_id: str, prompt: str) -> dict:
    """The reply to an auto send: delivered now, or queued under the same handle."""
    result = job.to_dict()
    if queued:
        result["delivery"] = "queued"
        daemon.store.bus_append(
            "agent.queue_followup",
            from_id=caller_id,
            to_id=target_id,
            payload={"handle": job.handle, "prompt": prompt[:200]},
        )
    else:
        _publish_routed_send_event(
            daemon, caller_id=caller_id, target_id=target_id, handle=job.handle, prompt=prompt
        )
    return result


async def _send_after(
    daemon,
    params: dict,
    target_id: str,
    caller_id: str,
    task: str,
    response_format,
    skills: str | None = None,
    on_reserved: Callable[[str, str | None], None] | None = None,
) -> dict:
    """Send once the source job ends: queued behind it, or with its card when it already ended."""
    handoff = daemon.handoff
    source, source_name = handoff.resolve_send_source(params, task)
    if source.state == JobState.RUNNING:
        job = await daemon.controls.queue_gated_send(
            target_id,
            caller_id=caller_id,
            prompt=compose_prompt(task, skills=skills, response_format=response_format),
            response_format=response_format,
            gate=handoff.send_gate(
                source, source_name, task=task, response_format=response_format, skills=skills
            ),
        )
        result = _auto_result(daemon, job, True, caller_id, target_id, task)
        result["handoff"] = handoff.describe(job.handle)
        return result
    prompt = handoff.send_prompt_now(source, source_name, task, response_format, skills)
    job, queued = await daemon.controls.send_when_safe(
        target_id,
        caller_id=caller_id,
        prompt=prompt,
        response_format=response_format,
        on_reserved=on_reserved,
    )
    result = _auto_result(daemon, job, queued, caller_id, target_id, prompt)
    result["handoff"] = {"state": "opened", "task_kind": "work", "source": source.handle}
    return result


@method("send")
async def _send(daemon, params: dict) -> dict:
    """Send through the daemon's selected route: once when safe, else queued under one handle.

    Explicit ``refuse`` is open to anyone; ``queue``, ``amend`` and ``replace`` stay parent-only.
    A play position among the skills is granted only once this message is delivered.
    """
    from theater.daemon.rpc import redirect

    target = daemon.registry.resolve(_require(params, "target"))
    target_id = target.id
    if_busy = redirect.if_busy_param(params)
    caller_id = params.get("caller_id") or "cli"
    response_format = _serialized_response_format(daemon, params, creator_id=caller_id)
    task = _require(params, "prompt")
    attachment = attach_skills(daemon, params, task=task, sender=caller_id)
    skills = attachment.block
    prompt = compose_prompt(task, skills=skills, response_format=response_format)

    refuse = functools.partial(_refuse_send, daemon, caller_id=caller_id, target_id=target_id)
    try:
        assignment = daemon.plays.check_send(caller_id, target_id, attachment.plan)
    except TheaterError as exc:
        refuse(exc, reason=exc.refusal_reason or exc.code)
    # Held from reservation, before delivery, so the accepting settlement always finds it.
    hold = daemon.plays.holder(assignment)

    async def deliver(decorate: Callable[[str], str] | None = None) -> dict:
        text = prompt if decorate is None else decorate(prompt)
        job = await daemon.controls.send(
            target_id,
            caller_id=caller_id,
            prompt=text,
            response_format=response_format,
            on_reserved=hold,
            defer_provider_presence=if_busy != "refuse",
        )
        if (
            if_busy == "refuse"
            and job.state == JobState.CRASHED
            and job.error_code == "human_present"
        ):
            raise HumanPresent(
                job.result or "human present at provider delivery",
                details={"next": NEXT_HUMAN_PRESENT},
            )
        if any(
            operation.delivery_phase is ControlDeliveryPhase.QUEUED
            for operation in daemon.store.control_operations_for_job(job.handle)
        ):
            return _auto_result(daemon, job, True, caller_id, target_id, text)
        _publish_routed_send_event(
            daemon,
            caller_id=caller_id,
            target_id=target_id,
            handle=job.handle,
            prompt=text,
        )
        return job.to_dict()

    async def plain() -> dict:
        result = await deliver()
        if if_busy != "refuse":
            result["route"] = "queued" if result.get("delivery") == "queued" else "send"
        return result

    async def route() -> dict:
        if params.get("after") is not None or params.get("task_kind") is not None:
            return await _send_after(
                daemon, params, target_id, caller_id, task, response_format, skills, hold
            )
        if if_busy == "auto":
            job, queued = await daemon.controls.send_when_safe(
                target_id,
                caller_id=caller_id,
                prompt=prompt,
                response_format=response_format,
                on_reserved=hold,
            )
            return _auto_result(daemon, job, queued, caller_id, target_id, prompt)
        if if_busy == "refuse":
            return await plain()
        redirect.authorize_redirect(daemon, target_id, caller_id)
        try:
            return await plain()
        except Busy:
            pass
        routed = {**params, "target": target_id, "caller_id": caller_id}
        if if_busy == "queue":
            return await redirect.queue(daemon, routed)
        if if_busy == "amend":
            return await redirect.amend(daemon, routed, send=plain)
        return await redirect.replace(daemon, routed, send=deliver)

    try:
        result = await route()
    except TheaterError as exc:
        refuse(exc, reason=exc.refusal_reason or exc.code)
    if assignment is not None:
        daemon.plays.track_assignment(assignment, result["handle"])
    return result
