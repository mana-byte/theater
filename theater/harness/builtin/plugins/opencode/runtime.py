"""Native send runtime hosted by the ordinary OpenCode TUI process."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping

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
    RuntimeRequestError,
    RuntimeSettings,
    RuntimeSnapshot,
    RuntimeWiring,
    SessionOpenMode,
)

from .live import OpenCodeTuiLiveSource

_CONTROL_TIMEOUT_SECONDS = 10.0
_PROMPT_MAX_CHARS = 60_000
# Encoded size must fit the daemon's frontend request line with headroom.
_PROMPT_MAX_BYTES = 60_000
# Only definite client-side pre-mutation plugin errors can follow an
# unapplied prompt, so they stay REJECTED; server-side and ambiguous
# failures are UNKNOWN, never replayed.
_REJECTED_ERROR_CODES = frozenset(
    {
        "invalid_request",
        "not_ready",
        "wrong_session",
        "busy",
        "operation_in_progress",
    }
)
_NATIVE_TURN_ID_PREFIX = "msg_"


class OpenCodeFrontendRuntime(HarnessRuntime):
    """Native send over the trusted stock-TUI session; everything else stays gated."""

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
        raise RuntimeError("the OpenCode TUI runtime does not own a frontend launch plan")

    def live_source(self) -> OpenCodeTuiLiveSource:
        self._start_receiver()
        return self._source

    async def snapshot(self) -> RuntimeSnapshot:
        self._start_receiver()
        health = self._source.connection_health
        scope = self._source.control_scope()
        execution_state = self._source.current_execution_state()
        capabilities = self._capabilities(health, scope)
        return RuntimeSnapshot(
            participant_id=self.context.participant_id,
            backend_generation=self.context.backend_generation,
            native_session_id=scope[0] if scope is not None else None,
            native_turn_id=self._source.active_turn_id() if scope is not None else None,
            settings=RuntimeSettings(),
            capabilities=capabilities,
            health=health,
            health_diagnostics=()
            if health is ConnectionHealth.CONNECTED
            else ("OpenCode TUI bridge has no current trusted session status",),
            execution_state=execution_state,
        )

    async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
        if (
            not isinstance(prompt, str)
            or not prompt.strip()
            or len(prompt) > _PROMPT_MAX_CHARS
            or len(prompt.encode("utf-8")) > _PROMPT_MAX_BYTES
        ):
            return _receipt(
                operation_id,
                DeliveryResult.REJECTED,
                error_code="invalid_request",
                error="prompt must be a bounded non-blank string",
            )
        scope = self._source.control_scope()
        if scope is None:
            return _receipt(
                operation_id,
                DeliveryResult.REJECTED,
                error_code="not_ready",
                error="the OpenCode TUI bridge has no current trusted session",
            )
        session_id, epoch = scope
        if self._source.current_execution_state() is not RuntimeExecutionState.IDLE:
            return _receipt(
                operation_id,
                DeliveryResult.REJECTED,
                error_code="busy",
                error="the OpenCode session is not idle; Theater queues the input instead",
            )
        try:
            result = await self._connection.request(
                "opencode.send",
                {
                    "operation_id": operation_id,
                    "native_session_id": session_id,
                    "prompt": prompt,
                },
                timeout=_CONTROL_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except RuntimeRequestError as exc:
            code = exc.code if isinstance(exc.code, str) else ""
            if code in _REJECTED_ERROR_CODES:
                return _receipt(
                    operation_id, DeliveryResult.REJECTED, error_code=code, error=exc.message
                )
            return _unknown(operation_id, exc.message)
        except Exception as exc:
            # Timeout, disconnect or malformed reply: the prompt may have crossed.
            return _unknown(operation_id, str(exc))
        try:
            native_turn_id = _decode_send_result(result, operation_id, session_id, epoch)
        except ValueError as exc:
            return _unknown(operation_id, str(exc))
        if self._source.control_scope() != scope or not self._source.note_submitted_turn(
            session_id, epoch, native_turn_id
        ):
            return _unknown(
                operation_id,
                "the trusted OpenCode session changed while the prompt was in flight",
            )
        return _receipt(operation_id, DeliveryResult.ACCEPTED, native_turn_id=native_turn_id)

    async def steer(
        self,
        *,
        operation_id: str,
        native_turn_id: str,
        prompt: str,
    ) -> ControlReceipt:
        del native_turn_id, prompt
        return _gated(
            operation_id,
            "busy OpenCode input stays in Theater's queue; steering has no public surface",
        )

    async def interrupt(
        self,
        *,
        operation_id: str,
        native_turn_id: str | None = None,
    ) -> ControlReceipt:
        del native_turn_id
        return _gated(
            operation_id,
            "OpenCode exposes no expected-turn abort; interrupt stays on its legacy route",
        )

    async def update_settings(
        self,
        *,
        operation_id: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ControlReceipt:
        del model, reasoning_effort
        return _gated(
            operation_id,
            "the OpenCode TUI runtime has no settings surface",
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._connection.aclose()
        if self._receiver is not None:
            self._receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._receiver

    def _capabilities(
        self,
        health: ConnectionHealth,
        scope: tuple[str, int] | None,
    ) -> RuntimeCapabilities:
        available: frozenset[RuntimeCapability] = frozenset()
        reasons: dict[RuntimeCapability, CapabilityUnavailableReason] = dict.fromkeys(
            (
                RuntimeCapability.INTERRUPT,
                RuntimeCapability.STEER,
                RuntimeCapability.SETTINGS_UPDATE,
            ),
            CapabilityUnavailableReason.THEATER_POLICY,
        )
        # Only a connected bridge with the exact trusted scope may mutate; a
        # degraded or stale status is never send-capable.
        if health is ConnectionHealth.CONNECTED and scope is not None:
            available = available | {RuntimeCapability.SEND, RuntimeCapability.QUEUE_FOLLOWUP}
        else:
            reason = (
                CapabilityUnavailableReason.GATED_BY_BACKEND
                if health in {ConnectionHealth.UNOPENED, ConnectionHealth.DISCONNECTED}
                else CapabilityUnavailableReason.SESSION_STATE
            )
            reasons[RuntimeCapability.SEND] = reason
            reasons[RuntimeCapability.QUEUE_FOLLOWUP] = reason
        return RuntimeCapabilities(available=available, unavailable_reasons=reasons)

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


def _receipt(
    operation_id: str,
    result: DeliveryResult,
    *,
    native_turn_id: str | None = None,
    error_code: str | None = None,
    error: str | None = None,
) -> ControlReceipt:
    return ControlReceipt(
        operation_id=operation_id,
        result=result,
        native_turn_id=native_turn_id,
        error_code=error_code,
        error=error,
    )


def _unknown(operation_id: str, detail: str) -> ControlReceipt:
    return _receipt(
        operation_id,
        DeliveryResult.UNKNOWN,
        error_code="delivery_unknown",
        error=(
            "the OpenCode prompt delivery became uncertain and Theater did not replay it"
            f" ({detail[:256]})"
        ),
    )


def _gated(operation_id: str, detail: str) -> ControlReceipt:
    return _receipt(
        operation_id,
        DeliveryResult.REJECTED,
        error_code="theater_policy",
        error=detail,
    )


def _decode_send_result(
    result: Mapping[str, object],
    operation_id: str,
    session_id: str,
    epoch: int,
) -> str:
    """Strict reply decode; any drift is UNKNOWN, never a guessed acceptance."""

    def malformed(detail: str) -> ValueError:
        return ValueError(detail)

    if result.get("status") != "accepted":
        raise malformed("the OpenCode send reply is not an acceptance")
    if result.get("operation_id") != operation_id:
        raise malformed("the OpenCode send reply names another operation")
    if result.get("native_session_id") != session_id:
        raise malformed("the OpenCode send reply names another session")
    native_turn_id = result.get("native_turn_id")
    if (
        not isinstance(native_turn_id, str)
        or not native_turn_id.startswith(_NATIVE_TURN_ID_PREFIX)
        or len(native_turn_id) > 128
    ):
        raise malformed("the OpenCode send reply has no valid native turn id")
    session_epoch = result.get("session_epoch")
    if type(session_epoch) is not int or session_epoch != epoch:
        raise malformed("the OpenCode send reply carries another session epoch")
    return native_turn_id


def opencode_frontend_runtime_factory(context: RuntimeContext) -> HarnessRuntime:
    return OpenCodeFrontendRuntime(context)


__all__ = ["OpenCodeFrontendRuntime", "opencode_frontend_runtime_factory"]
