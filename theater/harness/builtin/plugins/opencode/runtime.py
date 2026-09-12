"""Passive runtime hosted by the ordinary OpenCode TUI process."""

from __future__ import annotations

import asyncio
import contextlib

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
    RuntimeSettings,
    RuntimeSnapshot,
    RuntimeWiring,
    SessionOpenMode,
)

from .live import OpenCodeTuiLiveSource


class OpenCodeFrontendRuntime(HarnessRuntime):
    """Receive passive TUI observations without changing stock session behavior."""

    def __init__(self, context: RuntimeContext) -> None:
        if context.frontend is None:
            raise ValueError("OpenCode frontend runtime requires a frontend connection")
        if context.trusted_session_id_provider is None:
            raise ValueError(
                "OpenCode frontend runtime requires a trusted session identity provider"
            )
        self.context = context
        self._connection = context.frontend
        self._source = OpenCodeTuiLiveSource(context.trusted_session_id_provider)
        self._closed = False
        self._receiver: asyncio.Task[None] | None = None

    async def open_session(
        self,
        *,
        mode: SessionOpenMode,
        native_session_id: str | None = None,
    ) -> RuntimeBinding:
        del mode, native_session_id
        return RuntimeBinding(
            participant_id=self.context.participant_id,
            backend_generation=self.context.backend_generation,
            wiring=RuntimeWiring.NATIVE,
            lifecycle=RuntimeLifecyclePhase.ATTACHED,
            endpoint=self.context.endpoint,
        )

    async def frontend_plan(self, *, native_session_id: str | None = None) -> LaunchPlan:
        del native_session_id
        raise RuntimeError("the passive OpenCode runtime does not own a frontend launch plan")

    def live_source(self) -> OpenCodeTuiLiveSource:
        self._start_receiver()
        return self._source

    async def snapshot(self) -> RuntimeSnapshot:
        self._start_receiver()
        return RuntimeSnapshot(
            participant_id=self.context.participant_id,
            backend_generation=self.context.backend_generation,
            settings=RuntimeSettings(),
            capabilities=RuntimeCapabilities(
                unavailable_reasons=dict.fromkeys(
                    RuntimeCapability,
                    CapabilityUnavailableReason.THEATER_POLICY,
                )
            ),
            health=self._source.connection_health,
            health_diagnostics=()
            if self._source.connection_health is ConnectionHealth.CONNECTED
            else ("passive OpenCode TUI has no current trusted session status",),
            execution_state=RuntimeExecutionState.UNKNOWN,
        )

    async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
        del prompt
        return _rejected(operation_id)

    async def steer(
        self,
        *,
        operation_id: str,
        native_turn_id: str,
        prompt: str,
    ) -> ControlReceipt:
        del native_turn_id, prompt
        return _rejected(operation_id)

    async def interrupt(
        self,
        *,
        operation_id: str,
        native_turn_id: str | None = None,
    ) -> ControlReceipt:
        del native_turn_id
        return _rejected(operation_id)

    async def update_settings(
        self,
        *,
        operation_id: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ControlReceipt:
        del model, reasoning_effort
        return _rejected(operation_id)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._connection.aclose()
        if self._receiver is not None:
            self._receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._receiver

    def _start_receiver(self) -> None:
        if self._receiver is not None or self._closed:
            return
        self._receiver = asyncio.get_running_loop().create_task(self._receive())

    async def _receive(self) -> None:
        try:
            async for notification in self._connection.notifications():
                self._source.feed(notification)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self._source.disconnected()


def _rejected(operation_id: str) -> ControlReceipt:
    return ControlReceipt(
        operation_id=operation_id,
        result=DeliveryResult.REJECTED,
        error_code="theater_policy",
        error="OpenCode TUI observations are passive; this control stays on its legacy route",
    )


def opencode_frontend_runtime_factory(context: RuntimeContext) -> HarnessRuntime:
    return OpenCodeFrontendRuntime(context)


__all__ = ["OpenCodeFrontendRuntime", "opencode_frontend_runtime_factory"]
