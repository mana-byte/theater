"""Authenticated generic hook ingress."""

from __future__ import annotations

import asyncio
import hmac
from dataclasses import dataclass

from theater.constants.daemon import BUS_KIND_AGENT_HARNESS_EVENT
from theater.constants.harness import HARNESS_HOOK_TOKEN_MAX_CHARS
from theater.daemon.rpc.params import _optional_string_param, _string_param
from theater.daemon.rpc.router import method
from theater.harness import normalize
from theater.harness.channels.hooks import (
    HookIngressError,
    validate_hook_identifier,
    validate_hook_payload,
)
from theater.harness.contracts.callbacks import HookAdmissionIdentity, HookCorrelationContext
from theater.harness.contracts.channels import ChannelKind, HookBinding
from theater.harness.contracts.manifest import HookChannelManifest
from theater.models import BadRequest, Participant, Status

HARNESS_EVENT_RPC = "harness.event"


@dataclass(frozen=True, slots=True)
class _HookRequest:
    pid: str
    token: str
    channel_id: str
    event: str
    delivery_id: str | None


def _identity_lost(daemon, participant_id: str) -> bool:
    """Fail closed when the observer cannot report identity quarantine."""
    observer = getattr(daemon, "observer", None)
    checker = getattr(observer, "transcript_identity_lost", None)
    if not callable(checker):
        return True
    try:
        return bool(checker(participant_id))
    except Exception:
        return True


def _hook_identity_snapshot(daemon, participant) -> HookAdmissionIdentity | None:
    """Take the raw persisted identity snapshot for one hook admission.

    Runs on the event loop, so no path resolution here: harness callbacks compare canonical
    paths in their bounded worker.
    """
    if participant is None or participant.status is Status.DEAD:
        return None
    return HookAdmissionIdentity(
        harness=participant.harness if isinstance(participant.harness, str) else None,
        session_id=participant.session_id if isinstance(participant.session_id, str) else None,
        session_correlation=(
            participant.session_correlation
            if isinstance(participant.session_correlation, str)
            else None
        ),
        transcript_location=(
            participant.transcript_location
            if isinstance(participant.transcript_location, str)
            else None
        ),
        identity_lost=_identity_lost(daemon, participant.id),
    )


def _hook_channel(observer, channel_id: str):
    for manifest in observer.enrichment_manifests():
        if isinstance(manifest, HookChannelManifest) and manifest.declaration.id == channel_id:
            return manifest
    return None


async def _accepted_native_id(runtime, binding, context: HookCorrelationContext) -> str:
    try:
        native_id = await runtime.correlate(binding, context)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise BadRequest("harness event correlation is invalid") from None
    try:
        return validate_hook_identifier(native_id, "native_id")
    except HookIngressError:
        raise BadRequest("harness event correlation is invalid") from None


@method(HARNESS_EVENT_RPC)
async def _harness_event(daemon, params: dict) -> dict:
    """Authenticate one declared native hook envelope."""
    request = _parse_hook_request(params)
    participant = _admit_participant(daemon, params, request)
    channel, binding = _admit_binding(daemon, participant, request)
    payload = _validate_payload(params, channel)
    if request.delivery_id is not None and daemon.hook_runtime.delivery_seen(
        participant_id=request.pid,
        channel_id=request.channel_id,
        delivery_id=request.delivery_id,
    ):
        return {"ok": True, "duplicate": True, "dropped": False}
    identity = _hook_identity_snapshot(daemon, participant)
    if identity is None:
        raise BadRequest("harness event id names a dead participant")
    native_id = await _accepted_native_id(
        daemon.hook_runtime,
        binding,
        HookCorrelationContext(
            participant_id=request.pid,
            channel_id=request.channel_id,
            event=request.event,
            payload=payload,
            delivery_id=request.delivery_id,
            expected_session_id=identity.session_id,
            expected_transcript_location=identity.transcript_location,
            expected_session_provenance=identity.session_correlation,
            identity_lost=identity.identity_lost,
        ),
    )
    if _hook_identity_snapshot(daemon, daemon.store.get_participant(request.pid)) != identity:
        raise BadRequest("harness event participant identity changed during correlation")
    result = daemon.hook_runtime.enqueue(
        participant_id=request.pid,
        channel=channel,
        event=request.event,
        payload=payload,
        delivery_id=request.delivery_id,
        native_id=native_id,
        admission_identity=identity,
    )
    _publish_hook_event(daemon, request, result)
    return {"ok": True, "duplicate": result.duplicate, "dropped": result.dropped}


def _parse_hook_request(params: dict) -> _HookRequest:
    pid = _string_param(params, "id", method_name=HARNESS_EVENT_RPC)
    token = _string_param(params, "token", method_name=HARNESS_EVENT_RPC)
    channel_id = _string_param(params, "channel", method_name=HARNESS_EVENT_RPC)
    event = _string_param(params, "event", method_name=HARNESS_EVENT_RPC)
    delivery_id = _optional_string_param(params, "delivery_id", method_name=HARNESS_EVENT_RPC)
    if not token.strip() or len(token) > HARNESS_HOOK_TOKEN_MAX_CHARS or not token.isascii():
        raise BadRequest("harness event credential is invalid")
    try:
        validate_hook_identifier(channel_id, "channel")
        validate_hook_identifier(event, "event")
        if delivery_id is not None:
            validate_hook_identifier(delivery_id, "delivery_id")
    except HookIngressError as exc:
        raise BadRequest(str(exc)) from exc
    return _HookRequest(pid, token, channel_id, event, delivery_id)


def _admit_participant(daemon, params: dict, request: _HookRequest) -> Participant:
    participant = daemon.store.get_participant(request.pid)
    if participant is None:
        raise BadRequest("harness event id does not name an existing participant")
    if participant.status is Status.DEAD:
        daemon.store.delete_channel_credentials(request.pid)
        raise BadRequest("harness event id names a dead participant")
    supplied_harness = params.get("harness")
    if supplied_harness is not None:
        if not isinstance(supplied_harness, str):
            raise BadRequest("harness event parameter 'harness' must be a string or null")
        if supplied_harness != participant.harness:
            raise BadRequest("harness event harness does not match the participant")
    credential = daemon.store.get_channel_credential(
        request.pid, ChannelKind.HOOK, request.channel_id
    )
    if (
        credential is None
        or credential.harness != participant.harness
        or credential.channel_id != request.channel_id
        or not hmac.compare_digest(request.token, credential.token)
    ):
        raise BadRequest("harness event credential is invalid")
    return participant


def _admit_binding(
    daemon, participant: Participant, request: _HookRequest
) -> tuple[HookChannelManifest, HookBinding]:
    harness = daemon.observer.harnesses.get(normalize(participant.harness))
    observer = getattr(harness, "observer", None) if harness is not None else None
    if observer is None:
        raise BadRequest("harness event has no observer for the participant harness")
    channel = _hook_channel(observer, request.channel_id)
    if channel is None or channel.unavailable_reason is not None or not channel.bindings:
        raise BadRequest("harness event channel is not enabled")
    binding = next(
        (binding for binding in channel.bindings if binding.event == request.event), None
    )
    if binding is None:
        raise BadRequest("harness event is not declared for this channel")
    return channel, binding


def _validate_payload(params: dict, channel: HookChannelManifest) -> dict[str, object]:
    try:
        return validate_hook_payload(
            params.get("payload"), max_bytes=channel.declaration.bounds.max_payload_bytes
        )
    except HookIngressError as exc:
        raise BadRequest(str(exc)) from exc


def _publish_hook_event(daemon, request: _HookRequest, result) -> None:
    if not result.duplicate:
        daemon.store.bus_append(
            BUS_KIND_AGENT_HARNESS_EVENT,
            to_id=request.pid,
            payload={
                "channel": request.channel_id,
                "event": request.event,
                "dropped": result.dropped,
            },
        )


__all__ = ["HARNESS_EVENT_RPC", "_harness_event"]
