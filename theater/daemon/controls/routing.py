"""Capability routing for legacy panes and native runtime bindings."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from theater.daemon.runtime.wiring import runtime_manifest_of
from theater.harness import get as get_harness
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ControlTransport,
    RuntimeCapability,
    RuntimeManifest,
    RuntimeWiring,
)


@dataclass(frozen=True, slots=True)
class ControlRoute:
    """The selected transport for one capability, if Theater may offer it."""

    capability: RuntimeCapability
    transport: ControlTransport | None
    native_wiring: bool
    unavailable_reason: CapabilityUnavailableReason | None = None

    @property
    def is_native(self) -> bool:
        return self.transport is ControlTransport.NATIVE_RUNTIME

    @property
    def is_legacy(self) -> bool:
        return self.transport is ControlTransport.LEGACY_TMUX


class ControlRouteResolver:
    """Resolve one operation without making a harness-specific policy decision."""

    def __init__(self, *, store, runtime_for) -> None:
        self._store = store
        self._runtime_for = runtime_for

    def resolve(self, participant_id: str, capability: RuntimeCapability) -> ControlRoute:
        binding = self._store.get_runtime_binding(participant_id)
        runtime = self._runtime_for(participant_id)
        native_wiring = (binding is not None and binding.wiring is RuntimeWiring.NATIVE) or (
            binding is None and runtime is not None
        )
        if native_wiring and binding is not None:
            pinned = _pinned_route(binding.launch_policy, capability)
            if pinned is not None:
                return pinned
        harness_name = (
            binding.harness if binding is not None else self._harness_name(participant_id)
        )
        manifest = self._manifest(harness_name)
        if native_wiring or runtime is not None:
            if manifest is not None and capability in manifest.legacy_fallback:
                return ControlRoute(capability, ControlTransport.LEGACY_TMUX, native_wiring=True)
            if manifest is not None and capability in manifest.unavailable_capabilities:
                return ControlRoute(
                    capability,
                    None,
                    native_wiring=True,
                    unavailable_reason=CapabilityUnavailableReason.THEATER_POLICY,
                )
            return ControlRoute(capability, ControlTransport.NATIVE_RUNTIME, native_wiring=True)
        if capability in (
            RuntimeCapability.SEND,
            RuntimeCapability.QUEUE_FOLLOWUP,
            RuntimeCapability.INTERRUPT,
        ):
            return ControlRoute(capability, ControlTransport.LEGACY_TMUX, native_wiring=False)
        return ControlRoute(capability, None, native_wiring=False)

    def _harness_name(self, participant_id: str) -> str:
        participant = self._store.get_participant(participant_id)
        return participant.harness if participant is not None else ""

    @staticmethod
    def _manifest(harness_name: str):
        try:
            harness = get_harness(harness_name)
        except Exception:
            return None
        return runtime_manifest_of(harness)


def manifest_control_routes(manifest: RuntimeManifest) -> dict[str, str | None]:
    """Snapshot capability transport decisions for this launch, without secrets."""
    return {
        capability.value: (
            ControlTransport.LEGACY_TMUX.value
            if capability in manifest.legacy_fallback
            else None
            if capability in manifest.unavailable_capabilities
            else ControlTransport.NATIVE_RUNTIME.value
        )
        for capability in RuntimeCapability
    }


def _pinned_route(policy: str | None, capability: RuntimeCapability) -> ControlRoute | None:
    try:
        value = json.loads(policy) if policy else {}
    except ValueError:
        value = {}
    if not isinstance(value, Mapping) or "control_routes" not in value:
        return None  # Bindings created before per-capability routing.
    routes = value["control_routes"]
    transport = routes.get(capability.value) if isinstance(routes, Mapping) else None
    if isinstance(transport, str) and transport in {
        ControlTransport.NATIVE_RUNTIME.value,
        ControlTransport.LEGACY_TMUX.value,
    }:
        return ControlRoute(capability, ControlTransport(transport), native_wiring=True)
    return ControlRoute(
        capability,
        None,
        native_wiring=True,
        unavailable_reason=CapabilityUnavailableReason.THEATER_POLICY,
    )


__all__ = ["ControlRoute", "ControlRouteResolver", "manifest_control_routes"]
