"""Pi frontend session and connection lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    RuntimeBinding,
    RuntimeLifecyclePhase,
    RuntimeSnapshot,
    RuntimeWiring,
    SessionOpenMode,
)
from theater.harness.contracts.source import Source

from ._runtime_host import PiFrontendRuntimeHost
from .live_source import PiFrontendLiveSource
from .runtime_constants import (
    PI_FRONTEND_CONTROL_TIMEOUT_SECONDS,
)
from .runtime_protocol import (
    PiFrontendPeer,
    _bounded_string,
    _decode_snapshot,
)


class PiFrontendRuntimeSession(PiFrontendRuntimeHost):
    _peer: PiFrontendPeer | None
    _receive_task: asyncio.Task[None] | None
    _live_source: Source | None
    _closed: bool

    async def open_session(
        self, *, mode: SessionOpenMode, native_session_id: str | None = None
    ) -> RuntimeBinding:
        if mode is not SessionOpenMode.RECONNECT:
            raise RuntimeError("the stock Pi frontend already owns its live session")
        snapshot = await self.attach(native_session_id=native_session_id)
        return RuntimeBinding(
            participant_id=self._participant_id,
            backend_generation=self._backend_generation,
            native_session_id=snapshot.native_session_id,
            wiring=RuntimeWiring.NATIVE,
            lifecycle=RuntimeLifecyclePhase.ATTACHED,
            endpoint=self._endpoint,
        )

    async def frontend_plan(self, *, native_session_id: str | None = None) -> LaunchPlan:
        raise RuntimeError("Pi's ordinary launch plan owns its stock frontend")

    async def attach(self, *, native_session_id: str | None = None) -> RuntimeSnapshot:
        """Confirm the exact live Pi session before exposing native settings."""
        expected = native_session_id or self._expected_native_session_id
        if expected is not None:
            _bounded_string(expected, "expected native session id")
        snapshot = await self.snapshot()
        if snapshot.health is not ConnectionHealth.CONNECTED or snapshot.native_session_id is None:
            raise RuntimeError("Pi frontend bridge did not confirm a live native session")
        if expected is not None and snapshot.native_session_id != expected:
            raise RuntimeError(
                "Pi frontend bridge attached a different native session "
                f"({snapshot.native_session_id!r} != {expected!r}); refusing to bind"
            )
        self._start_receiver()
        return snapshot

    async def reconnect(self, peer: PiFrontendPeer) -> RuntimeSnapshot:
        """Explicitly install a replacement host peer without replaying mutations."""
        if not isinstance(peer, PiFrontendPeer):
            raise TypeError("Pi frontend peer must implement PiFrontendPeer")
        old_peer = self._peer
        old_task = self._receive_task
        self._peer_generation += 1
        self._peer = peer
        self._receive_task = None
        # A replacement host may be attached to a newly loaded extension whose
        # local epoch restarts at one.  Only this explicit peer replacement may
        # discard the prior epoch/sequence; an ordinary delayed notification
        # never gets that authority.
        self._reset_for_peer_reconnect()
        if old_task is not None:
            old_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await old_task
        if old_peer is not None and old_peer is not peer:
            with contextlib.suppress(Exception):
                await old_peer.aclose()
        return await self.attach(native_session_id=self._expected_native_session_id)

    def live_source(self) -> Source:
        """Return one status-only live source; durable Pi JSONL remains authoritative."""
        if self._live_source is None:
            self._live_source = PiFrontendLiveSource(self)
        self._start_receiver()
        return self._live_source

    async def snapshot(self) -> RuntimeSnapshot:
        """Read an exact current bridge snapshot, failing closed on transport loss."""
        peer = self._peer
        if peer is None or self._closed:
            return self._runtime_snapshot()
        peer_generation = self._peer_generation
        try:
            result = await peer.request(
                "pi.snapshot", {}, timeout=PI_FRONTEND_CONTROL_TIMEOUT_SECONDS
            )
            decoded = _decode_snapshot(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._mark_disconnected(f"pi.snapshot failed: {type(exc).__name__}: {exc}")
            return self._runtime_snapshot()
        if peer is not self._peer or peer_generation != self._peer_generation:
            self._mark_disconnected("Pi frontend peer changed while snapshot was in flight")
            return self._runtime_snapshot()
        if not self._apply_snapshot(decoded):
            self._diagnostic("Pi frontend rejected a stale or conflicting snapshot")
            self._health = ConnectionHealth.DEGRADED
            self._settings_available = False
            self._touch()
        return self._runtime_snapshot()

    async def aclose(self) -> None:
        """Release the host connection without affecting the stock Pi process."""
        if self._closed:
            return
        self._closed = True
        self._peer_generation += 1
        task, self._receive_task = self._receive_task, None
        peer, self._peer = self._peer, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if peer is not None:
            with contextlib.suppress(Exception):
                await peer.aclose()
        self._mark_disconnected("Pi frontend runtime was closed")

    def set_activity_callback(self, callback: Callable[[], None] | None) -> None:
        """Install the observer's optional arrival wake callback."""
        self._activity_callback = callback
