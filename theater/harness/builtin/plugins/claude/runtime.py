"""One Claude function-hook connection; native receipts, never terminal input."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Mapping, Sequence

from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ConnectionHealth,
    ControlReceipt,
    DeliveryResult,
    HarnessRuntime,
    RuntimeBinding,
    RuntimeCapabilities,
    RuntimeCapability,
    RuntimeContext,
    RuntimeExecutionState,
    RuntimeLifecyclePhase,
    RuntimeSnapshot,
    RuntimeWiring,
    SessionOpenMode,
)
from theater.harness.contracts.source import Source

from .compatibility import VERSION_POLICY
from .live_source import ClaudeLiveSource
from .runtime_protocol import (
    CONTROL_TIMEOUT_SECONDS,
    MAX_PENDING_OUTCOMES,
    PROTOCOL,
    Snapshot,
    optional_text,
    outcome,
    snapshot,
)


class ClaudeRuntime(HarnessRuntime):
    def __init__(self, context: RuntimeContext) -> None:
        if context.frontend is None:
            raise ValueError("Claude native wiring needs its authenticated frontend connection")
        self.context = context
        self.peer = context.frontend
        self.native_session_id = context.native_session_id
        self._snapshot: Snapshot | None = None
        self._connected_at = time.time()
        self._closed = False
        self._error: str | None = None
        self._reader: asyncio.Task[None] | None = None
        self._acks: set[asyncio.Task[None]] = set()
        self._delivered: set[str] = set()
        self.revision = 0
        self.source = ClaudeLiveSource(self)

    def _changed(self) -> None:
        self.revision += 1
        self.source.changed()

    def _trusted(self, session_id: str) -> bool:
        provider = self.context.trusted_session_id_provider
        try:
            return provider is None or provider() == session_id
        except Exception:
            return False

    def _accept_snapshot(self, data: Mapping[str, object]) -> None:
        value = snapshot(data)
        if not VERSION_POLICY.admits(value.version):
            raise ValueError(
                f"Claude bridge version {value.version} is outside the qualified range"
            )
        old = self._snapshot
        if old is not None and (value.generation != old.generation):
            raise ValueError("Claude bridge instance changed or its snapshot regressed")
        if old is not None and value.revision < old.revision:
            return
        self._error = None
        if old != value:
            self._snapshot = value
            self._changed()

    def current_snapshot(self) -> RuntimeSnapshot:
        value = self._snapshot
        health = ConnectionHealth.CONNECTED
        error = self._error or (None if value is None else value.error)
        trusted = (
            value is not None
            and value.current_session_id == value.session_id
            and self._trusted(value.session_id)
        )
        if self._closed or self.peer.closed:
            health = ConnectionHealth.DISCONNECTED
        elif value is None:
            health = ConnectionHealth.UNOPENED
        elif not trusted or value.session_id != self.native_session_id or error:
            health = ConnectionHealth.DEGRADED
        available = (
            frozenset({RuntimeCapability.SEND, RuntimeCapability.INTERRUPT})
            if health is ConnectionHealth.CONNECTED
            else frozenset()
        )
        return RuntimeSnapshot(
            participant_id=self.context.participant_id,
            backend_generation=self.context.backend_generation,
            native_session_id=None if value is None else value.session_id,
            native_turn_id=(
                value.active_turn_id
                if value is not None and health is ConnectionHealth.CONNECTED
                else None
            ),
            execution_state=(
                value.state
                if value is not None and health is ConnectionHealth.CONNECTED
                else RuntimeExecutionState.UNKNOWN
            ),
            capabilities=RuntimeCapabilities(
                available=available,
                unavailable_reasons={
                    RuntimeCapability.STEER: CapabilityUnavailableReason.THEATER_POLICY,
                    RuntimeCapability.SETTINGS_UPDATE: CapabilityUnavailableReason.THEATER_POLICY,
                },
            ),
            health=health,
            health_diagnostics=() if error is None else (error[:512],),
        )

    async def open_session(
        self, *, mode: SessionOpenMode, native_session_id: str | None = None
    ) -> RuntimeBinding:
        if mode is not SessionOpenMode.RECONNECT:
            raise ValueError("Claude's stock frontend owns session creation and resume")
        self._reader = asyncio.create_task(self._receive())
        data = await self.peer.request("claude.snapshot", {}, timeout=CONTROL_TIMEOUT_SECONDS)
        self._accept_snapshot(data)
        assert self._snapshot is not None
        expected = native_session_id or self.native_session_id
        actual = self._snapshot.session_id
        if expected is not None and expected != actual:
            raise ValueError("Claude frontend attached a different native session")
        if not self._trusted(self._snapshot.current_session_id):
            raise ValueError("Claude frontend does not match the daemon's trusted session")
        self.native_session_id = actual
        return RuntimeBinding(
            participant_id=self.context.participant_id,
            backend_generation=self.context.backend_generation,
            native_session_id=actual,
            native_version=self._snapshot.version,
            wiring=RuntimeWiring.NATIVE,
            lifecycle=RuntimeLifecyclePhase.ATTACHED,
            endpoint=self.context.endpoint,
            protocol=PROTOCOL,
        )

    async def frontend_plan(self, *, native_session_id: str | None = None) -> LaunchPlan:
        raise RuntimeError("Claude's ordinary launch planner owns the native UI")

    def live_source(self) -> Source:
        return self.source

    async def snapshot(self) -> RuntimeSnapshot:
        if not self._closed and not self.peer.closed:
            try:
                data = await self.peer.request(
                    "claude.snapshot", {}, timeout=CONTROL_TIMEOUT_SECONDS
                )
                self._accept_snapshot(data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._error = f"Claude native snapshot unavailable: {type(exc).__name__}: {exc}"[
                    :512
                ]
                self._changed()
        return self.current_snapshot()

    async def _control(self, method: str, operation_id: str, **params: object) -> ControlReceipt:
        before = await self.snapshot()
        value = self._snapshot
        if before.health is not ConnectionHealth.CONNECTED or value is None:
            return ControlReceipt(
                operation_id,
                DeliveryResult.REJECTED,
                error_code="not_ready",
                error="Claude native connection is not ready",
            )
        try:
            result = await self.peer.request(
                method,
                {
                    "operation_id": operation_id,
                    "session_id": self.native_session_id,
                    "generation": value.generation,
                    **params,
                },
                timeout=CONTROL_TIMEOUT_SECONDS,
            )
            return self._receipt(result, operation_id, value.generation)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ControlReceipt(
                operation_id,
                DeliveryResult.UNKNOWN,
                error_code="delivery_unknown",
                error=(
                    f"Claude native delivery uncertain; not replayed: {type(exc).__name__}: {exc}"
                )[:512],
            )

    def _receipt(
        self, result: Mapping[str, object], operation_id: str, generation: int
    ) -> ControlReceipt:
        if (
            result.get("operation_id") != operation_id
            or result.get("session_id") != self.native_session_id
            or result.get("generation") != generation
            or self.peer.closed
        ):
            raise ValueError("Claude control reply crossed an operation, session or instance")
        return ControlReceipt(
            operation_id=operation_id,
            result=DeliveryResult(str(result.get("result"))),
            native_turn_id=optional_text(result.get("native_turn_id"), "receipt turn"),
            error_code=optional_text(result.get("error_code"), "receipt error code"),
            error=optional_text(result.get("error"), "receipt error"),
        )

    async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
        return await self._control("claude.send", operation_id, prompt=prompt)

    async def interrupt(
        self, *, operation_id: str, native_turn_id: str | None = None
    ) -> ControlReceipt:
        if native_turn_id is None:
            return ControlReceipt(
                operation_id,
                DeliveryResult.REJECTED,
                error_code="stale_target",
                error="Claude interrupt requires the exact active native turn",
            )
        return await self._control("claude.interrupt", operation_id, native_turn_id=native_turn_id)

    async def steer(self, *, operation_id: str, native_turn_id: str, prompt: str) -> ControlReceipt:
        return self._unsupported(operation_id)

    async def update_settings(
        self, *, operation_id: str, model: str | None = None, reasoning_effort: str | None = None
    ) -> ControlReceipt:
        return self._unsupported(operation_id)

    @staticmethod
    def _unsupported(operation_id: str) -> ControlReceipt:
        return ControlReceipt(
            operation_id,
            DeliveryResult.REJECTED,
            error_code="unsupported",
            error="This native Claude control is not supported",
        )

    async def _receive(self) -> None:
        try:
            async for notification in self.peer.notifications():
                if notification.method == "snapshot":
                    self._accept_snapshot(notification.params)
                elif notification.method == "history":
                    self._accept_history(notification.params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = f"Claude native observation failed: {type(exc).__name__}: {exc}"[:512]
            await self.peer.aclose()
        finally:
            self._changed()

    def _accept_history(self, params: Mapping[str, object]) -> None:
        records = params.get("outcomes")
        if (
            not isinstance(records, Sequence)
            or isinstance(records, str)
            or len(records) > MAX_PENDING_OUTCOMES
        ):
            raise ValueError("invalid Claude outcome history")
        for item in records:
            if not isinstance(item, Mapping):
                raise TypeError("Claude outcome must be an object")
            evidence = outcome(item, connected_at=self._connected_at)
            expected = self.native_session_id or (
                self._snapshot.session_id if self._snapshot else None
            )
            if evidence.native_session_id != expected:
                raise ValueError("Claude outcome belongs to another session")
            if evidence.native_turn_id in self._delivered:
                self.acknowledge((evidence.native_turn_id,))
            else:
                self.source.append(evidence)

    def acknowledge(self, turns: tuple[str, ...]) -> None:
        self._delivered.update(turns)
        # The native buffer is bounded; an old duplicate can safely revisit the idempotent sink.
        if len(self._delivered) > 128:
            self._delivered = set(turns)
        if not self._closed and not self.peer.closed:
            task = asyncio.create_task(self._ack(turns))
            self._acks.add(task)
            task.add_done_callback(self._acks.discard)

    async def _ack(self, turns: tuple[str, ...]) -> None:
        with contextlib.suppress(Exception):
            await self.peer.request(
                "claude.ack",
                {"session_id": self.native_session_id, "turn_ids": turns},
                timeout=CONTROL_TIMEOUT_SECONDS,
            )

    async def aclose(self) -> None:
        self._closed = True
        tasks = [*self._acks, *([self._reader] if self._reader is not None else [])]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.peer.aclose()
        self._changed()


def claude_runtime_factory(context: RuntimeContext) -> HarnessRuntime:
    return ClaudeRuntime(context)
