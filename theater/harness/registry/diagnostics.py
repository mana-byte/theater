"""Manifest-derived harness diagnostics and bounded runtime projections."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from theater.constants.harness import (
    HARNESS_DIAGNOSTICS_MAX_BINDINGS,
    HARNESS_DIAGNOSTICS_MAX_CHANNELS,
    HARNESS_DIAGNOSTICS_MAX_PARTICIPANTS,
    HARNESS_DIAGNOSTICS_UNAVAILABLE_REASON_MAX_CHARS,
)
from theater.harness.contracts.channels import ChannelDeclaration, ChannelHealth
from theater.harness.contracts.manifest import (
    HookChannelManifest,
    OtelChannelManifest,
    UnavailableChannelManifest,
)
from theater.harness.loading.models import LoadedPlugin

type HarnessRuntimeHealth = Mapping[str, Mapping[str, Sequence[ChannelHealth]]]


def project_plugin(
    row: Mapping[str, object],
    plugin: LoadedPlugin,
    runtime: HarnessRuntimeHealth | None,
) -> dict:
    """Add manifest and runtime diagnostics to one existing harness row."""
    projected = dict(row)
    manifest = plugin.manifest
    if manifest is None:
        return projected
    primary = manifest.observation.primary
    declarations: tuple[object, ...] = () if primary is None else (primary.channel,)
    declarations += manifest.observation.enrichments
    channels, channels_omitted = _limited(
        (_channel(channel) for channel in declarations), HARNESS_DIAGNOSTICS_MAX_CHANNELS
    )
    projected.update(
        {
            "package_path": str(plugin.path),
            "manifest_path": str(plugin.path / "manifest.py"),
            "manifest_api_version": manifest.api_version,
            "primary_channel": None if primary is None else _channel(primary.channel),
            "channels": channels,
            "runtime": _runtime(
                runtime.get(plugin.name, {}) if runtime is not None else {},
                {channel.id for channel in manifest.observation.channels},
            ),
        }
    )
    if channels_omitted:
        projected["channels_omitted"] = channels_omitted
    return projected


def _channel(channel: object) -> dict[str, Any]:
    if isinstance(channel, ChannelDeclaration):
        return _declaration(channel, availability="declared")
    if isinstance(channel, HookChannelManifest):
        result = _declaration(
            channel.declaration,
            availability="unavailable" if channel.unavailable_reason is not None else "declared",
        )
        hook_bindings, omitted = _limited(channel.bindings, HARNESS_DIAGNOSTICS_MAX_BINDINGS)
        result["bindings"] = [
            {
                "event": binding.event,
                "delivery": binding.delivery.value,
                "signals": [signal.value for signal in binding.signals],
            }
            for binding in hook_bindings
        ]
        if omitted:
            result["bindings_omitted"] = omitted
        if channel.unavailable_reason is not None:
            result["unavailable_reason"] = _bounded_reason(channel.unavailable_reason)
        return result
    if isinstance(channel, OtelChannelManifest):
        result = _declaration(
            channel.declaration,
            availability="unavailable" if channel.unavailable_reason is not None else "declared",
        )
        result["protocol"] = channel.protocol.value
        otel_bindings, omitted = _limited(channel.bindings, HARNESS_DIAGNOSTICS_MAX_BINDINGS)
        result["bindings"] = [
            {
                "name": binding.name,
                "signal": binding.signal.value,
                "signals": [signal.value for signal in binding.signals],
            }
            for binding in otel_bindings
        ]
        if omitted:
            result["bindings_omitted"] = omitted
        if channel.unavailable_reason is not None:
            result["unavailable_reason"] = _bounded_reason(channel.unavailable_reason)
        return result
    if isinstance(channel, UnavailableChannelManifest):
        result = _declaration(channel.declaration, availability="unavailable")
        result["unavailable_reason"] = _bounded_reason(channel.reason)
        return result
    raise TypeError("validated manifest has an unknown channel declaration")


def _declaration(channel: ChannelDeclaration, *, availability: str) -> dict[str, object]:
    return {
        "id": channel.id,
        "kind": channel.kind.value,
        "availability": availability,
        "capabilities": [
            {"signal": capability.signal.value, "ownership": capability.ownership.value}
            for capability in channel.capabilities
        ],
    }


def _runtime(
    health_by_participant: Mapping[str, Sequence[ChannelHealth]],
    declared_channel_ids: set[str],
) -> dict[str, object]:
    participants: list[dict[str, object]] = []
    for participant_id, health in sorted(health_by_participant.items()):
        channels, channels_omitted = _limited(
            (
                _health(channel)
                for channel in sorted(health, key=lambda item: item.channel_id)
                if channel.channel_id in declared_channel_ids
            ),
            HARNESS_DIAGNOSTICS_MAX_CHANNELS,
        )
        if not channels:
            continue
        participant: dict[str, object] = {"participant_id": participant_id, "channels": channels}
        if channels_omitted:
            participant["channels_omitted"] = channels_omitted
        participants.append(participant)
    participants, participants_omitted = _limited(
        participants, HARNESS_DIAGNOSTICS_MAX_PARTICIPANTS
    )
    projected: dict[str, object] = {
        "state": "active" if participants else "inactive",
        "participants": participants,
    }
    if participants_omitted:
        projected["participants_omitted"] = participants_omitted
    return projected


def _health(health: ChannelHealth) -> dict[str, object]:
    return {
        "id": health.channel_id,
        "state": health.state.value,
        "diagnostics": list(health.diagnostics),
        "dropped": health.dropped,
        "accepted": health.accepted,
        "last_success_at": health.last_success_at,
    }


def _limited[T](values: Iterable[T], limit: int) -> tuple[list[T], int]:
    projected: list[T] = []
    omitted = 0
    for value in values:
        if len(projected) < limit:
            projected.append(value)
        else:
            omitted += 1
    return projected, omitted


def _bounded_reason(reason: str) -> str:
    normalized = " ".join("".join(char if char.isprintable() else " " for char in reason).split())
    if len(normalized) <= HARNESS_DIAGNOSTICS_UNAVAILABLE_REASON_MAX_CHARS:
        return normalized
    omitted = len(normalized) - HARNESS_DIAGNOSTICS_UNAVAILABLE_REASON_MAX_CHARS
    marker = f"… (+{omitted} chars omitted)"
    keep = HARNESS_DIAGNOSTICS_UNAVAILABLE_REASON_MAX_CHARS - len(marker)
    return f"{normalized[:keep]}{marker}"


__all__ = ["HarnessRuntimeHealth", "project_plugin"]
