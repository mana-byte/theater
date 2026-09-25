"""Pi's additive stock-extension frontend runtime.

Optional: the durable JSONL and legacy pane controls stay independent of this connection.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping

from theater.harness.contracts.runtime import (
    ConnectionHealth,
    HarnessRuntime,
    NativeHumanInteraction,
    RuntimeContext,
    RuntimeExecutionState,
    RuntimeFrontendConnection,
    RuntimeSettings,
)

from .live_source import PiFrontendLiveSource
from .runtime_constants import (
    PI_FRONTEND_CHANNEL_ID,
    PI_FRONTEND_CONTROL_TIMEOUT_SECONDS,
    PI_FRONTEND_DIAGNOSTICS_MAX,
    PI_FRONTEND_RUNTIME_COMPATIBILITY_POLICY,
)
from .runtime_controls import PiFrontendRuntimeControls
from .runtime_notifications import PiFrontendRuntimeNotifications
from .runtime_probe import parse_pi_version, probe_pi_frontend_compatibility
from .runtime_protocol import PiFrontendPeer, PiFrontendProtocolError, _bounded_string
from .runtime_results import PiFrontendRuntimeResults
from .runtime_session import PiFrontendRuntimeSession
from .runtime_state import PiFrontendRuntimeState


class PiFrontendRuntime(
    PiFrontendRuntimeSession,
    PiFrontendRuntimeControls,
    PiFrontendRuntimeResults,
    PiFrontendRuntimeNotifications,
    PiFrontendRuntimeState,
    HarnessRuntime,
):
    """One Pi stock-UI extension session over an injected frontend peer.

    Model mutation and ``steer`` refuse explicitly (proof-gated) so they never silently
    replace Theater's legacy paths.
    """

    def __init__(
        self,
        *,
        participant_id: str,
        backend_generation: int,
        peer: PiFrontendPeer,
        expected_native_session_id: str | None = None,
        native_version: str | None = None,
        trusted_session_id_provider: Callable[[], str | None] | None = None,
        endpoint: str | None = None,
    ) -> None:
        _bounded_string(participant_id, "participant id")
        if type(backend_generation) is not int or backend_generation < 0:
            raise ValueError("Pi frontend backend_generation must be a non-negative integer")
        if not isinstance(peer, PiFrontendPeer):
            raise TypeError("Pi frontend peer must implement PiFrontendPeer")
        if expected_native_session_id is not None:
            _bounded_string(expected_native_session_id, "expected native session id")
        if native_version is not None:
            _bounded_string(native_version, "native version")
        self._participant_id = participant_id
        self._backend_generation = backend_generation
        self._peer: PiFrontendPeer | None = peer
        self._peer_generation = 0
        self._expected_native_session_id = expected_native_session_id
        self._native_version = native_version
        self._trusted_session_id_provider = trusted_session_id_provider
        self._endpoint = endpoint
        self._native_session_id: str | None = None
        self._native_turn_id: str | None = None
        self._bridge_epoch: int | None = None
        self._snapshot_revision: int | None = None
        self._settings = RuntimeSettings()
        self._execution_state = RuntimeExecutionState.UNKNOWN
        self._pending_interaction: NativeHumanInteraction | None = None
        self._settings_available = False
        self._send_available = False
        self._interrupt_available = False
        self._health = ConnectionHealth.UNOPENED
        self._diagnostics: deque[str] = deque(maxlen=PI_FRONTEND_DIAGNOSTICS_MAX)
        self._receive_task: asyncio.Task[None] | None = None
        self._live_source: PiFrontendLiveSource | None = None
        self._activity_callback: Callable[[], None] | None = None
        self._session_epoch = 0
        self._last_sequence = -1
        self._revision = 0
        self._accepted = 0
        self._dropped = 0
        self._closed = False


class _HostedPiPeer:
    """Adapt generic host notifications to Pi's independently tested wire decoder."""

    def __init__(self, connection: RuntimeFrontendConnection) -> None:
        self._connection = connection

    async def request(
        self, method: str, params: Mapping[str, object], *, timeout: float
    ) -> Mapping[str, object]:
        return await self._connection.request(method, params, timeout=timeout)

    async def notifications(self) -> AsyncIterator[Mapping[str, object]]:
        async for notification in self._connection.notifications():
            yield {"type": notification.method, **notification.params}

    async def aclose(self) -> None:
        await self._connection.aclose()


def pi_frontend_runtime_factory(context: RuntimeContext) -> HarnessRuntime:
    if context.frontend is None or context.trusted_session_id_provider is None:
        raise ValueError("Pi frontend runtime requires an authenticated peer and trusted identity")
    return PiFrontendRuntime(
        participant_id=context.participant_id,
        backend_generation=context.backend_generation,
        peer=_HostedPiPeer(context.frontend),
        trusted_session_id_provider=context.trusted_session_id_provider,
        endpoint=context.endpoint,
    )


__all__ = [
    "PI_FRONTEND_CHANNEL_ID",
    "PI_FRONTEND_CONTROL_TIMEOUT_SECONDS",
    "PI_FRONTEND_RUNTIME_COMPATIBILITY_POLICY",
    "PiFrontendLiveSource",
    "PiFrontendPeer",
    "PiFrontendProtocolError",
    "PiFrontendRuntime",
    "parse_pi_version",
    "pi_frontend_runtime_factory",
    "probe_pi_frontend_compatibility",
]
