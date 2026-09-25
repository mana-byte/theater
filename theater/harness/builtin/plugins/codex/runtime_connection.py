"""Codex native connection and session lifecycle."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping

from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    RuntimeBinding,
    RuntimeConnection,
    RuntimeConnectionClosed,
    RuntimeConnectionError,
    RuntimeExecutionState,
    RuntimeLifecyclePhase,
    RuntimeSnapshot,
    RuntimeWiring,
    SessionOpenMode,
)
from theater.harness.contracts.source import Source

from . import runtime_constants
from ._runtime_host import CodexRuntimeHost
from .live_source import CodexLiveSource
from .runtime_constants import (
    CODEX_RUNTIME_CONTROL_TIMEOUT_SECONDS,
    CODEX_RUNTIME_RECONCILE_TURNS,
    _initialize_params,
)
from .runtime_messages import (
    _bounded_str,
    _resume_params,
    _same_cwd,
    _thread_id_of,
    _thread_status_type,
    _version_from_user_agent,
)
from .runtime_plan import (
    CODEX_RUNTIME_COMPATIBILITY_POLICY,
    CODEX_RUNTIME_VERIFIED_VERSIONS,
    codex_thread_config_overrides,
    plan_codex_frontend,
)


class CodexRuntimeConnection(CodexRuntimeHost):
    _connection: RuntimeConnection | None
    _receive_task: asyncio.Task[None] | None
    _history_reconcile_task: asyncio.Task[None] | None
    _subscription_recovery_task: asyncio.Task[None] | None
    _live_source: Source | None
    _native_session_id: str | None
    _thread_status: str | None
    _health: ConnectionHealth
    _native_version: str | None

    async def open_session(
        self,
        *,
        mode: SessionOpenMode,
        native_session_id: str | None = None,
    ) -> RuntimeBinding:
        if self._native_session_id is not None:
            raise RuntimeError("codex runtime session is already opened")
        if not isinstance(mode, SessionOpenMode):
            raise TypeError("open_session mode must be a SessionOpenMode")
        await self._connect()
        if mode is SessionOpenMode.NEW:
            session_id = await self._open_ui_created_session(native_session_id)
        elif mode is SessionOpenMode.FORK:
            session_id = await self._open_forked_session(native_session_id)
        elif mode is SessionOpenMode.RECONNECT:
            session_id = await self._open_reconnected_session(native_session_id)
        else:
            raise ValueError(f"unsupported session open mode: {mode!r}")
        self._native_session_id = session_id
        await self._probe_settings_gate()
        if mode is SessionOpenMode.RECONNECT:
            self._start_history_reconciliation(session_id)
        return self._binding()

    async def _open_ui_created_session(self, native_session_id: str | None) -> str:
        if native_session_id is not None:
            raise ValueError(
                "open_session(NEW) opens the UI-created session; pass native_session_id=None"
            )
        thread = await self._await_ui_thread()
        session_id = _thread_id_of(thread)
        if session_id is None:
            raise RuntimeConnectionError(
                "thread/started broadcast carried no usable native thread id"
            )
        self._thread_status = _thread_status_type(thread)
        # A zero-turn thread has no rollout yet: thread/resume would fail with "no rollout found".
        return session_id

    async def _open_forked_session(self, native_session_id: str | None) -> str:
        parent = _bounded_str(native_session_id, limit=512)
        if parent is None:
            raise ValueError("open_session(FORK) requires the exact parent native session id")
        params: dict[str, object] = {"threadId": parent}
        params.update(
            codex_thread_config_overrides(
                approval=self.context.approval,
                model=self.context.model,
                reasoning_effort=self.context.reasoning_effort,
            )
        )
        result = await self._request("thread/fork", params)
        forked = result.get("thread") if isinstance(result, Mapping) else None
        forked_thread: Mapping[str, object] | None = forked if isinstance(forked, Mapping) else None
        session_id = _thread_id_of(forked_thread)
        if session_id is None:
            raise RuntimeConnectionError("thread/fork did not return the forked native thread id")
        self._thread_status = _thread_status_type(forked_thread) if forked_thread else None
        # Bind the exact fork before explicit resume subscription; requester subscription is not
        # presumed.
        self._native_session_id = session_id
        await self._subscribe_after_rollout()
        return session_id

    async def _open_reconnected_session(self, native_session_id: str | None) -> str:
        expected = _bounded_str(native_session_id, limit=512)
        if expected is None:
            raise ValueError("open_session(RECONNECT) requires the exact native session id")
        # Ask the verified app-server for only the same tiny current-state page that the synchronous
        # reconciliation consumes.
        result = await self._request("thread/resume", _resume_params(expected))
        await self._reconcile_resume_result(result, expected)
        return expected

    async def _reconcile_resume_result(self, result: Mapping[str, object], expected: str) -> None:
        resumed = result.get("thread") if isinstance(result, Mapping) else None
        resumed_thread: Mapping[str, object] | None = (
            resumed if isinstance(resumed, Mapping) else None
        )
        attached = _thread_id_of(resumed_thread)
        if attached != expected:
            # Identity mismatch fails closed; never attach by cwd resemblance.
            raise RuntimeConnectionError(
                "thread/resume attached a different native thread "
                f"({attached!r} != {expected!r}); refusing to bind"
            )
        page = result.get("initialTurnsPage")
        turns = page.get("data") if isinstance(page, Mapping) else None
        if not isinstance(turns, (list, tuple)) or len(turns) > CODEX_RUNTIME_RECONCILE_TURNS:
            raise RuntimeConnectionError(
                "thread/resume did not return the requested bounded initialTurnsPage; "
                "refusing full-history hydration or an incomplete current-state view"
            )
        if resumed_thread is not None:
            self._subscribed = True
            # The separate page is newest-first; reconciliation consumes an
            # oldest-first bounded view. Never fall back to thread.turns.
            await self._reconcile_thread(resumed_thread, expected, turns=tuple(reversed(turns)))

    async def frontend_plan(self, *, native_session_id: str | None = None) -> LaunchPlan:
        endpoint = self.context.endpoint
        if not endpoint:
            raise ValueError("codex frontend plan requires the private backend endpoint")
        # Initialize the observer before returning the promptless UI plan so eager thread/start
        # cannot be missed.
        await self._connect()
        return plan_codex_frontend(
            endpoint,
            native_session_id=native_session_id,
            approval=self.context.approval,
            model=self.context.model,
            reasoning_effort=self.context.reasoning_effort,
        )

    def live_source(self) -> Source:
        # Share one Source per runtime so status cursors and terminal-buffer ownership cannot
        # compete.
        if self._live_source is None:
            self._live_source = CodexLiveSource(self)
        return self._live_source

    async def snapshot(self) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            participant_id=self.context.participant_id,
            backend_generation=self.context.backend_generation,
            native_session_id=self._native_session_id,
            native_turn_id=self._active_turn_id,
            pending_interaction=self._pending_interaction,
            settings=self._settings,
            capabilities=self._capabilities(),
            health=self._health,
            health_diagnostics=tuple(self._diagnostics),
            execution_state=self._execution_state(),
        )

    def _execution_state(self) -> RuntimeExecutionState:
        """Plugin-confirmed execution state from native Codex state only."""
        if self._active_turn_id is not None or self._thread_status == "active":
            return RuntimeExecutionState.ACTIVE
        if (
            self._thread_status == "idle"
            and self._native_session_id is not None
            and self._health in (ConnectionHealth.CONNECTED, ConnectionHealth.DEGRADED)
        ):
            return RuntimeExecutionState.IDLE
        return RuntimeExecutionState.UNKNOWN

    async def aclose(self) -> None:
        """Disconnect Theater's connection only; never terminate the backend."""
        receive_task = self._receive_task
        self._receive_task = None
        history_task = self._history_reconcile_task
        self._history_reconcile_task = None
        subscription_task = self._subscription_recovery_task
        self._subscription_recovery_task = None
        connection = self._connection
        self._connection = None
        tasks = (
            (receive_task, "receive loop"),
            (history_task, "history reconciliation"),
            (subscription_task, "subscription recovery"),
        )
        for task, _ in tasks:
            if task is not None and not task.done():
                task.cancel()
        for task, label in tasks:
            if task is None:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as error:  # pragma: no cover - defensive
                self._diagnostic(f"{label} ended: {error}")
        if connection is not None:
            try:
                await connection.aclose()
            except Exception as error:  # pragma: no cover - defensive
                self._diagnostic(f"connection close failed: {error}")
        self._subscribed = False
        self._health = ConnectionHealth.DISCONNECTED

    async def _abandon_connection(self, connection: RuntimeConnection) -> None:
        """Discard a connection whose handshake failed; never leak it."""
        self._connection = None
        try:
            await connection.aclose()
        except Exception as error:  # pragma: no cover - defensive
            self._diagnostic(f"handshake-failure close failed: {error}")

    async def _connect(self) -> None:
        if self._connection is not None:
            return
        endpoint = self.context.endpoint
        if not endpoint:
            raise ValueError("codex runtime requires the private backend endpoint")
        startup_timeout = runtime_constants.CODEX_RUNTIME_STARTUP_TIMEOUT_SECONDS
        connection = await self.context.io.connect(endpoint, timeout=startup_timeout)
        self._connection = connection
        try:
            result = await connection.request(
                "initialize", _initialize_params, timeout=startup_timeout
            )
        except BaseException:
            await self._abandon_connection(connection)
            raise
        native_version = _version_from_user_agent(
            result.get("userAgent") if isinstance(result, Mapping) else None
        )
        # Recheck the verified version at handshake; the binary may have changed since the
        # subprocess probe.
        if native_version not in CODEX_RUNTIME_VERIFIED_VERSIONS:
            await self._abandon_connection(connection)
            raise RuntimeConnectionError(
                "codex app-server handshake reported unverified native version "
                f"{native_version!r}; compatibility policy "
                f"{CODEX_RUNTIME_COMPATIBILITY_POLICY} verifies only "
                f"{', '.join(sorted(CODEX_RUNTIME_VERIFIED_VERSIONS))}"
            )
        try:
            # The dialect requires the initialized notification exactly once after initialize; no
            # jsonrpc field ever appears (the injected connection owns the wire framing).
            await connection.notify("initialized", {})
        except BaseException:
            await self._abandon_connection(connection)
            raise
        self._native_version = native_version
        self._health = ConnectionHealth.CONNECTED
        self._diagnostics.clear()
        self._receive_task = asyncio.create_task(
            self._receive_loop(), name=f"codex-runtime-{self.context.participant_id}"
        )

    async def _request(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        connection = self._require_connection()
        return await connection.request(
            method, params, timeout=CODEX_RUNTIME_CONTROL_TIMEOUT_SECONDS
        )

    def _require_connection(self) -> RuntimeConnection:
        if self._connection is None:
            raise RuntimeConnectionClosed("codex runtime connection is not open")
        return self._connection

    def _require_session(self) -> str:
        if self._native_session_id is None:
            raise RuntimeError("codex runtime session is not opened")
        return self._native_session_id

    async def _receive_loop(self) -> None:
        connection = self._connection
        if connection is None:
            return
        try:
            async for notification in connection.notifications():
                await self._handle_notification(notification)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._health = ConnectionHealth.DISCONNECTED
            self._diagnostic(f"native notification stream failed: {error}")
            # The health transition itself is readable state: wake the live source/observer so a
            # waiting reader notices the disconnect.
            self._notify_activity()
        else:
            self._health = ConnectionHealth.DISCONNECTED
            self._diagnostic("native notification stream ended")
            self._notify_activity()

    async def _await_ui_thread(self) -> Mapping[str, object]:
        """Wait for the exact UI-created thread on this private backend."""
        deadline = time.monotonic() + runtime_constants.CODEX_RUNTIME_STARTUP_TIMEOUT_SECONDS
        while True:
            candidates = [
                thread for thread in self._started_threads if thread.get("ephemeral") is not True
            ]
            cwd = self.context.cwd
            if cwd is not None:
                # Codex canonicalizes the broadcast cwd (``/private/tmp`` on macOS);
                # exact-string matching would miss it and the startup deadline expires.
                exact = [thread for thread in candidates if _same_cwd(thread.get("cwd"), cwd)]
                if len(exact) == 1:
                    return exact[0]
                if len(exact) > 1:
                    raise RuntimeConnectionError(
                        "multiple thread/started broadcasts match the participant cwd; "
                        "refusing to guess a native session"
                    )
            else:
                if len(candidates) == 1:
                    return candidates[0]
                if len(candidates) > 1:
                    raise RuntimeConnectionError(
                        "multiple thread/started broadcasts and no cwd predicate; "
                        "refusing to guess a native session"
                    )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeConnectionError(
                    "no thread/started broadcast from the private backend within the "
                    "startup deadline; launch the promptless native UI after the observer "
                    "connection initializes"
                )
            self._thread_started_event.clear()
            try:
                await asyncio.wait_for(self._thread_started_event.wait(), timeout=remaining)
            except TimeoutError:
                continue

    def _binding(self) -> RuntimeBinding:
        return RuntimeBinding(
            participant_id=self.context.participant_id,
            backend_generation=self.context.backend_generation,
            wiring=RuntimeWiring.NATIVE,
            lifecycle=RuntimeLifecyclePhase.BOUND,
            endpoint=self.context.endpoint,
            pid=None,
            native_session_id=self._native_session_id,
            protocol="codex-app-server",
            protocol_version=self._native_version,
            native_version=self._native_version,
            compatibility_policy=CODEX_RUNTIME_COMPATIBILITY_POLICY,
        )
