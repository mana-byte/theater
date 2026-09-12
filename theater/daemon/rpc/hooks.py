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
from theater.harness.contracts.callbacks import HookCorrelationContext
from theater.harness.contracts.channels import ChannelKind
from theater.harness.contracts.manifest import HookChannelManifest
from theater.models import BadRequest, Status
from theater.transcript_identity import canonical_location

HARNESS_EVENT_RPC = "harness.event"


@dataclass(frozen=True, slots=True)
class _HookIdentitySnapshot:
    """Daemon-owned identity facts that must not change during correlation."""

    session_id: str | None
    session_correlation: str | None
    transcript_location: str | None
    identity_lost: bool


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


def _hook_identity_snapshot(daemon, participant) -> _HookIdentitySnapshot:
    """Take the canonical trusted identity snapshot for one hook admission."""
    location = participant.transcript_location
    canonical = canonical_location(location) if isinstance(location, str) and location else location
    return _HookIdentitySnapshot(
        session_id=participant.session_id if isinstance(participant.session_id, str) else None,
        session_correlation=(
            participant.session_correlation
            if isinstance(participant.session_correlation, str)
            else None
        ),
        transcript_location=canonical if isinstance(canonical, str) else None,
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
async def _harness_event(daemon, params: dict) -> dict:  # noqa: PLR0912, PLR0915
    """Authenticate one declared native hook envelope."""
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
    identity = _hook_identity_snapshot(daemon, participant)
    native_id = await _accepted_native_id(
        daemon.hook_runtime,
        binding,
        HookCorrelationContext(
            participant_id=pid,
            channel_id=channel_id,
            event=event,
            payload=payload,
            delivery_id=delivery_id,
            expected_session_id=identity.session_id,
            expected_transcript_location=identity.transcript_location,
            expected_session_provenance=identity.session_correlation,
            identity_lost=identity.identity_lost,
        ),
    )
    current = daemon.store.get_participant(pid)
    if (
        current is None
        or current.status is Status.DEAD
        or _hook_identity_snapshot(daemon, current) != identity
    ):
        # Correlation runs off-loop.  Never enqueue a fact accepted against an
        # older transcript identity after the participant rotated, otherwise a
        # delayed native hook could be projected into the new source epoch.
        raise BadRequest("harness event participant identity changed during correlation")
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
