"""Typing-only contract for Pi frontend runtime concern mixins."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import asyncio
    from collections import deque
    from collections.abc import Callable

    from theater.harness.contracts.runtime import (
        ConnectionHealth,
        ControlReceipt,
        NativeHumanInteraction,
        RuntimeCapabilities,
        RuntimeExecutionState,
        RuntimeRequestError,
        RuntimeSettings,
        RuntimeSnapshot,
    )
    from theater.harness.contracts.source import Source

    from .runtime_protocol import PiFrontendPeer, _FrontendSnapshot

    class PiFrontendRuntimeHost(Protocol):
        """Shared state and cross-concern methods supplied by ``PiFrontendRuntime``."""

        _participant_id: str
        _backend_generation: int
        _peer: PiFrontendPeer | None
        _peer_generation: int
        _expected_native_session_id: str | None
        _native_version: str | None
        _trusted_session_id_provider: Callable[[], str | None] | None
        _endpoint: str | None
        _native_session_id: str | None
        _native_turn_id: str | None
        _bridge_epoch: int | None
        _snapshot_revision: int | None
        _settings: RuntimeSettings
        _execution_state: RuntimeExecutionState
        _pending_interaction: NativeHumanInteraction | None
        _settings_available: bool
        _send_available: bool
        _interrupt_available: bool
        _health: ConnectionHealth
        _diagnostics: deque[str]
        _receive_task: asyncio.Task[None] | None
        _live_source: Source | None
        _activity_callback: Callable[[], None] | None
        _session_epoch: int
        _last_sequence: int
        _revision: int
        _accepted: int
        _dropped: int
        _closed: bool

        async def snapshot(self) -> RuntimeSnapshot: ...
        def set_activity_callback(self, callback: Callable[[], None] | None) -> None: ...
        def _start_receiver(self) -> None: ...
        def _reset_for_peer_reconnect(self) -> None: ...
        def _apply_snapshot(self, snapshot: _FrontendSnapshot) -> bool: ...
        def _runtime_snapshot(self) -> RuntimeSnapshot: ...
        def _trusted_session_matches(self) -> bool: ...
        def _capabilities(self) -> RuntimeCapabilities: ...
        def _request_error_receipt(
            self, operation_id: str, exc: RuntimeRequestError, rejected: frozenset[str]
        ) -> ControlReceipt: ...
        def _decode_settings_result(self, value: object) -> dict[str, object]: ...
        def _decode_send_result(self, value: object) -> dict[str, str]: ...
        def _decode_interrupt_result(
            self,
            value: object,
            *,
            expected_operation_id: str,
            expected_session_id: str,
            expected_turn_id: str,
            expected_bridge_epoch: int,
        ) -> dict[str, str]: ...
        def _stream_identity_current(self, peer: PiFrontendPeer, peer_generation: int) -> bool: ...
        def _proof_gated(self, operation_id: str, control: str) -> ControlReceipt: ...
        def _rejected(self, operation_id: str, code: str, message: str) -> ControlReceipt: ...
        def _unknown(self, operation_id: str, code: str, message: str) -> ControlReceipt: ...
        def _mark_disconnected(self, diagnostic: str) -> None: ...
        def _diagnostic(self, value: str) -> None: ...
        def _touch(self) -> None: ...

else:
    PiFrontendRuntimeHost = object
