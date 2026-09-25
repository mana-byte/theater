"""Send RPC handler: preflight gates, pane identity, refusal, and delivery."""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import NoReturn

from theater.constants.daemon import BUS_KIND_SEND_REFUSED

# Definition re-exported by the methods facade; runtime reads the facade for legacy patches.
from theater.constants.daemon import SEND_CLAIM_TTL_SECONDS as SEND_CLAIM_TTL  # noqa: F401
from theater.daemon.rpc.params import (
    _prompt_with_response_format,
    _require,
    _serialized_response_format,
)
from theater.daemon.rpc.router import method
from theater.harness import HARNESSES, normalize
from theater.harness.contracts.runtime import ControlKind, ControlTransport, DeliveryResult
from theater.models import (
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
    """Record a send that never became a job, then raise it.

    Counted by `Store.refusal_counts`. Kept as one bus kind with a `reason`
    rather than one kind per refusal, so a reader can subscribe to all of them
    without knowing the list.
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


@method("send")
async def _send(daemon, params: dict) -> dict:
    """Send through the daemon's selected capability route."""
    target = daemon.registry.resolve(_require(params, "target"))
    target_id = target.id
    response_format = _serialized_response_format(params)
    prompt = _prompt_with_response_format(_require(params, "prompt"), response_format)
    caller_id = params.get("caller_id") or "cli"

    refuse = functools.partial(_refuse_send, daemon, caller_id=caller_id, target_id=target_id)

    try:
        job = await daemon.controls.send(
            target_id,
            caller_id=caller_id,
            prompt=prompt,
            response_format=response_format,
        )
    except Exception as exc:
        if isinstance(exc, TheaterError):
            refuse(exc, reason=exc.refusal_reason or exc.code)
        raise
    _publish_routed_send_event(
        daemon,
        caller_id=caller_id,
        target_id=target_id,
        handle=job.handle,
        prompt=prompt,
    )
    return job.to_dict()
