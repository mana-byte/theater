"""Authenticated generic hook ingress."""

from __future__ import annotations

import asyncio
import hmac

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
from theater.harness.contracts.callbacks import HookCorrelationContext
from theater.harness.contracts.channels import ChannelKind
from theater.harness.contracts.manifest import HookChannelManifest
from theater.models import BadRequest, Status

HARNESS_EVENT_RPC = "harness.event"


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
async def _harness_event(daemon, params: dict) -> dict:  # noqa: PLR0912
    """Authenticate one declared native hook envelope."""
    pid = _string_param(params, "id", method_name=HARNESS_EVENT_RPC)
    token = _string_param(params, "token", method_name=HARNESS_EVENT_RPC)
    channel_id = _string_param(params, "channel", method_name=HARNESS_EVENT_RPC)
    event = _string_param(params, "event", method_name=HARNESS_EVENT_RPC)
    delivery_id = _optional_string_param(params, "delivery_id", method_name=HARNESS_EVENT_RPC)
    if not token.strip() or len(token) > HARNESS_HOOK_TOKEN_MAX_CHARS:
        raise BadRequest("harness event credential is invalid")
    try:
        validate_hook_identifier(channel_id, "channel")
        validate_hook_identifier(event, "event")
        if delivery_id is not None:
            validate_hook_identifier(delivery_id, "delivery_id")
    except HookIngressError as exc:
        raise BadRequest(str(exc)) from exc
    participant = daemon.store.get_participant(pid)
    if participant is None:
        raise BadRequest("harness event id does not name an existing participant")
    if participant.status is Status.DEAD:
        daemon.store.delete_channel_credentials(pid)
        raise BadRequest("harness event id names a dead participant")
    supplied_harness = params.get("harness")
    if supplied_harness is not None:
        if not isinstance(supplied_harness, str):
            raise BadRequest("harness event parameter 'harness' must be a string or null")
        if supplied_harness != participant.harness:
            raise BadRequest("harness event harness does not match the participant")
    credential = daemon.store.get_channel_credential(pid, ChannelKind.HOOK, channel_id)
    if (
        credential is None
        or credential.harness != participant.harness
        or credential.channel_id != channel_id
        or not hmac.compare_digest(token, credential.token)
    ):
        raise BadRequest("harness event credential is invalid")
    harness = daemon.observer.harnesses.get(normalize(participant.harness))
    observer = getattr(harness, "observer", None) if harness is not None else None
    if observer is None:
        raise BadRequest("harness event has no observer for the participant harness")
    channel = _hook_channel(observer, channel_id)
    if channel is None or channel.unavailable_reason is not None or not channel.bindings:
        raise BadRequest("harness event channel is not enabled")
    binding = next((binding for binding in channel.bindings if binding.event == event), None)
    if binding is None:
        raise BadRequest("harness event is not declared for this channel")
    try:
        payload = validate_hook_payload(
            params.get("payload"), max_bytes=channel.declaration.bounds.max_payload_bytes
        )
    except HookIngressError as exc:
        raise BadRequest(str(exc)) from exc
    if delivery_id is not None and daemon.hook_runtime.delivery_seen(
        participant_id=pid, channel_id=channel_id, delivery_id=delivery_id
    ):
        return {"ok": True, "duplicate": True, "dropped": False}
    native_id = await _accepted_native_id(
        daemon.hook_runtime,
        binding,
        HookCorrelationContext(
            participant_id=pid,
            channel_id=channel_id,
            event=event,
            payload=payload,
            delivery_id=delivery_id,
        ),
    )
    result = daemon.hook_runtime.enqueue(
        participant_id=pid,
        channel=channel,
        event=event,
        payload=payload,
        delivery_id=delivery_id,
        native_id=native_id,
    )
    if not result.duplicate:
        daemon.store.bus_append(
            BUS_KIND_AGENT_HARNESS_EVENT,
            to_id=pid,
            payload={"channel": channel_id, "event": event, "dropped": result.dropped},
        )
    return {"ok": True, "duplicate": result.duplicate, "dropped": result.dropped}


__all__ = ["HARNESS_EVENT_RPC", "_harness_event"]
