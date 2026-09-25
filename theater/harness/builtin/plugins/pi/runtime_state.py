"""Pi frontend snapshots, capabilities, and shared state helpers."""

from __future__ import annotations

import contextlib

from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ConnectionHealth,
    NativeHumanInteraction,
    RuntimeCapabilities,
    RuntimeCapability,
    RuntimeExecutionState,
    RuntimeSettings,
    RuntimeSnapshot,
)

from ._runtime_host import PiFrontendRuntimeHost
from .frontend import PI_FRONTEND_MAX_VALUE_CHARS
from .runtime_protocol import (
    PiFrontendPeer,
)


class PiFrontendRuntimeState(PiFrontendRuntimeHost):
    _native_turn_id: str | None
    _pending_interaction: NativeHumanInteraction | None

    def _runtime_snapshot(self) -> RuntimeSnapshot:
        trusted = self._trusted_session_matches()
        health = self._health
        diagnostics = tuple(self._diagnostics)
        if not trusted and health is ConnectionHealth.CONNECTED:
            health = ConnectionHealth.DEGRADED
            diagnostics += ("Pi bridge session does not match the daemon's trusted session",)
        return RuntimeSnapshot(
            participant_id=self._participant_id,
            backend_generation=self._backend_generation,
            native_session_id=self._native_session_id,
            native_turn_id=self._native_turn_id if trusted else None,
            settings=self._settings if trusted else RuntimeSettings(),
            capabilities=self._capabilities(),
            health=health,
            health_diagnostics=diagnostics,
            execution_state=self._execution_state if trusted else RuntimeExecutionState.UNKNOWN,
            pending_interaction=self._pending_interaction if trusted else None,
        )

    def _trusted_session_matches(self) -> bool:
        provider = self._trusted_session_id_provider
        if provider is None:
            return True
        try:
            expected = provider()
        except Exception:
            return False
        return self._native_session_id is not None and self._native_session_id == expected

    def _capabilities(self) -> RuntimeCapabilities:
        available: set[RuntimeCapability] = set()
        bound = (
            self._health is ConnectionHealth.CONNECTED
            and self._native_session_id is not None
            and self._trusted_session_matches()
        )
        if bound and self._settings_available:
            available.add(RuntimeCapability.SETTINGS_UPDATE)
        if bound and self._send_available:
            available.add(RuntimeCapability.SEND)
        if bound and self._interrupt_available:
            available.add(RuntimeCapability.INTERRUPT)
        unavailable = {
            RuntimeCapability.STEER: CapabilityUnavailableReason.GATED_BY_BACKEND,
            RuntimeCapability.QUEUE_FOLLOWUP: CapabilityUnavailableReason.THEATER_POLICY,
        }
        if RuntimeCapability.SETTINGS_UPDATE not in available:
            unavailable[RuntimeCapability.SETTINGS_UPDATE] = (
                CapabilityUnavailableReason.GATED_BY_BACKEND
            )
        if RuntimeCapability.SEND not in available:
            unavailable[RuntimeCapability.SEND] = CapabilityUnavailableReason.GATED_BY_BACKEND
        if RuntimeCapability.INTERRUPT not in available:
            unavailable[RuntimeCapability.INTERRUPT] = CapabilityUnavailableReason.GATED_BY_BACKEND
        return RuntimeCapabilities(available=frozenset(available), unavailable_reasons=unavailable)

    def _stream_identity_current(self, peer: PiFrontendPeer, peer_generation: int) -> bool:
        return peer is self._peer and peer_generation == self._peer_generation

    def _mark_disconnected(self, diagnostic: str) -> None:
        self._diagnostic(diagnostic)
        self._health = ConnectionHealth.DISCONNECTED
        self._execution_state = RuntimeExecutionState.UNKNOWN
        self._pending_interaction = None
        self._settings_available = False
        self._send_available = False
        self._interrupt_available = False
        self._native_turn_id = None
        self._touch()

    def _diagnostic(self, value: str) -> None:
        self._diagnostics.append(value[:PI_FRONTEND_MAX_VALUE_CHARS])

    def _touch(self) -> None:
        self._revision += 1
        callback = self._activity_callback
        if callback is not None:
            with contextlib.suppress(Exception):
                callback()
