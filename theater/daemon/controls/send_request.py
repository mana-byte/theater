"""Ordinary send request admission, redirect, and delivery."""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import NoReturn

from theater.constants.daemon import BUS_KIND_SEND_REFUSED
from theater.constants.refusals import NEXT_HUMAN_PRESENT
from theater.daemon.controls import preflight, redirect
from theater.daemon.request_values import require
from theater.daemon.response_contracts import resolve_response_format
from theater.daemon.skill_attachments import attach_skills
from theater.daemon.skill_cards import compose_prompt
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
)

logger = logging.getLogger(__name__)

_transcript_identity_lost = preflight._transcript_identity_lost
_transcript_send_failure = preflight.transcript_send_failure
_check_transcript_send_preflight = preflight.check_transcript_send_preflight


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


async def send(daemon, params: dict) -> dict:
    """Send through the daemon's selected route: once when safe, else queued under one handle.

    Explicit ``refuse`` is open to anyone; ``queue``, ``amend`` and ``replace`` stay parent-only.
    A play position among the skills is granted only once this message is delivered.
    """
    target = daemon.registry.resolve(require(params, "target"))
    target_id = target.id
    if_busy = redirect.if_busy_param(params)
    caller_id = params.get("caller_id") or "cli"
    response_format = resolve_response_format(
        daemon.store, params.get("response_format"), creator_id=caller_id
    )
    task = require(params, "prompt")
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
