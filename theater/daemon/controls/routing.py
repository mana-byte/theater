"""Capability routing for provider terminals and native runtimes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from theater.daemon.harness_runtime.manager import CachedNativeRoute
from theater.daemon.runtime.launch_policy import decode_launch_policy
from theater.daemon.runtime.wiring import runtime_manifest_of
from theater.harness import get as get_harness
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ConnectionHealth,
    ControlTransport,
    RuntimeCapabilities,
    RuntimeCapability,
    RuntimeManifest,
    RuntimeWiring,
)
from theater.models import TerminalBindingRecord

ProviderHealth = Callable[[str, int], str]
NativeFacts = Callable[[str, object | None], CachedNativeRoute | None]


@dataclass(frozen=True, slots=True)
class ControlRoute:
    """The selected transport for one capability, if Theater may offer it."""

    capability: RuntimeCapability
    transport: ControlTransport | None
    native_wiring: bool
    unavailable_reason: CapabilityUnavailableReason | None = None
    terminal: TerminalBindingRecord | None = None
    provider_health: str | None = None
    native_facts: CachedNativeRoute | None = None
    unfenced_runtime_available: bool = False

    @property
    def native_capabilities(self) -> RuntimeCapabilities | None:
        return None if self.native_facts is None else self.native_facts.capabilities

    @property
    def is_native(self) -> bool:
        return self.terminal is None and self.transport is ControlTransport.NATIVE_RUNTIME

    @property
    def is_legacy(self) -> bool:
        return self.terminal is None and self.transport is ControlTransport.TERMINAL_FALLBACK

    @property
    def is_provider(self) -> bool:
        return self.transport is ControlTransport.PROVIDER_TERMINAL and self.terminal is not None

    @property
    def route_available(self) -> bool:
        if self.transport is ControlTransport.PROVIDER_TERMINAL:
            return (
                self.terminal is not None
                and self.terminal.health == "healthy"
                and self.provider_health == "online"
            )
        if self.transport is ControlTransport.NATIVE_RUNTIME:
            if self.native_facts is None:
                return self.unfenced_runtime_available
            return self.native_facts.health in {
                ConnectionHealth.CONNECTED,
                ConnectionHealth.DEGRADED,
            }
        if self.terminal is None:
            return self.transport is not None
        return False


class ControlRouteResolver:
    """Resolve one operation without making a harness-specific policy decision."""

    def __init__(
        self,
        *,
        store,
        runtime_for,
        provider_health: ProviderHealth | None = None,
        native_facts: NativeFacts | None = None,
    ) -> None:
        self._store = store
        self._runtime_for = runtime_for
        self._provider_health = provider_health or (lambda _provider_id, _generation: "offline")
        self._native_facts = native_facts

    def resolve(
        self, participant_id: str, capability: RuntimeCapability, *, connection=None
    ) -> ControlRoute:
        binding = self._store.get_runtime_binding(participant_id, connection=connection)
        runtime = self._runtime_for(participant_id)
        native_wiring = (binding is not None and binding.wiring is RuntimeWiring.NATIVE) or (
            binding is None and runtime is not None
        )
        if native_wiring and binding is not None:
            pinned = _pinned_route(binding.launch_policy, capability)
            if pinned is not None:
                return self._with_routes(
                    participant_id, pinned, binding=binding, runtime=runtime, connection=connection
                )
        harness_name = (
            binding.harness
            if binding is not None
            else self._harness_name(participant_id, connection=connection)
        )
        manifest = self._manifest(harness_name)
        if native_wiring or runtime is not None:
            if manifest is not None and capability in manifest.legacy_fallback:
                return self._with_routes(
                    participant_id,
                    ControlRoute(
                        capability, ControlTransport.PROVIDER_TERMINAL, native_wiring=True
                    ),
                    binding=binding,
                    runtime=runtime,
                    connection=connection,
                )
            if manifest is not None and capability in manifest.unavailable_capabilities:
                return ControlRoute(
                    capability,
                    None,
                    native_wiring=True,
                    unavailable_reason=CapabilityUnavailableReason.THEATER_POLICY,
                )
            return self._with_native_facts(
                participant_id,
                ControlRoute(capability, ControlTransport.NATIVE_RUNTIME, native_wiring=True),
                binding=binding,
                runtime=runtime,
            )
        if capability in (
            RuntimeCapability.SEND,
            RuntimeCapability.QUEUE_FOLLOWUP,
            RuntimeCapability.INTERRUPT,
        ):
            return self._with_provider_fallback(
                participant_id,
                ControlRoute(capability, ControlTransport.PROVIDER_TERMINAL, native_wiring=False),
                connection=connection,
            )
        if capability is RuntimeCapability.STEER and not self._declares_steer(harness_name):
            # Enter may only queue the text in that TUI; never offer a steer on a guess.
            return ControlRoute(
                capability,
                None,
                native_wiring=False,
                unavailable_reason=CapabilityUnavailableReason.WIRING_MODE,
            )
        return self._with_provider_fallback(
            participant_id,
            ControlRoute(capability, None, native_wiring=False),
            connection=connection,
        )

    def terminal_route(self, participant_id: str, *, connection=None) -> ControlRoute:
        return self._with_provider_fallback(
            participant_id,
            ControlRoute(RuntimeCapability.INTERRUPT, None, native_wiring=False),
            connection=connection,
        )

    def _with_routes(
        self, participant_id: str, route: ControlRoute, *, binding, runtime, connection
    ) -> ControlRoute:
        if route.is_native:
            return self._with_native_facts(participant_id, route, binding=binding, runtime=runtime)
        return self._with_provider_fallback(participant_id, route, connection=connection)

    def _with_native_facts(self, participant_id: str, route: ControlRoute, *, binding, runtime):
        if self._native_facts is None:
            # Directly injected runtimes in tests have no durable identity to fence.
            return replace(route, unfenced_runtime_available=runtime is not None)
        return replace(route, native_facts=self._native_facts(participant_id, binding))

    def _with_provider_fallback(
        self, participant_id: str, route: ControlRoute, *, connection=None
    ) -> ControlRoute:
        if (
            route.capability is RuntimeCapability.SETTINGS_UPDATE
            or route.is_native
            or route.unavailable_reason is not None
        ):
            return route
        repository = getattr(self._store, "terminal_bindings", None)
        binding = (
            repository.get(participant_id, connection=connection)
            if repository is not None
            else None
        )
        if binding is None:
            if route.transport in {
                ControlTransport.TERMINAL_FALLBACK,
                ControlTransport.PROVIDER_TERMINAL,
            }:
                return ControlRoute(
                    route.capability,
                    None,
                    native_wiring=route.native_wiring,
                    unavailable_reason=CapabilityUnavailableReason.WIRING_MODE,
                )
            return route
        return ControlRoute(
            route.capability,
            ControlTransport.PROVIDER_TERMINAL,
            native_wiring=route.native_wiring,
            unavailable_reason=route.unavailable_reason,
            terminal=binding,
            provider_health=self._provider_health(binding.provider_id, binding.provider_generation),
        )

    def _harness_name(self, participant_id: str, *, connection=None) -> str:
        participant = self._store.get_participant(participant_id, connection=connection)
        return participant.harness if participant is not None else ""

    @staticmethod
    def _declares_steer(harness_name: str) -> bool:
        try:
            controls = get_harness(harness_name).controls
        except Exception:
            return False
        return controls is not None and controls.steer is not None

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
            ControlTransport.PROVIDER_TERMINAL.value
            if capability in manifest.legacy_fallback
            else None
            if capability in manifest.unavailable_capabilities
            else ControlTransport.NATIVE_RUNTIME.value
        )
        for capability in RuntimeCapability
    }


def _pinned_route(policy: str | None, capability: RuntimeCapability) -> ControlRoute | None:
    facts = decode_launch_policy(policy)
    if not facts.control_routes_present:
        return None  # Bindings created before per-capability routing.
    routes = facts.control_routes
    transport = routes.get(capability.value) if routes is not None else None
    if transport == ControlTransport.NATIVE_RUNTIME.value:
        return ControlRoute(capability, ControlTransport.NATIVE_RUNTIME, native_wiring=True)
    if transport in {
        ControlTransport.TERMINAL_FALLBACK.value,
        ControlTransport.PROVIDER_TERMINAL.value,
    }:
        return ControlRoute(capability, ControlTransport.PROVIDER_TERMINAL, native_wiring=True)
    return ControlRoute(
        capability,
        None,
        native_wiring=True,
        unavailable_reason=CapabilityUnavailableReason.THEATER_POLICY,
    )


__all__ = [
    "ControlRoute",
    "ControlRouteResolver",
    "manifest_control_routes",
]
