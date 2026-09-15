"""Detached OpenCode server runtime: exact sessions and native send admission."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from pathlib import Path

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
from theater.harness.contracts.source import Source

from .http import OpenCodeClient, OpenCodeHttpError
from .runtime_plan import OPENCODE_SERVER_COMPATIBILITY_POLICY
from .server_live import OpenCodeServerLiveSource
from .server_plan import SERVER_SECRET_ENV

_CONFIRM_DEADLINE_SECONDS = 8.0
_RECONNECT_BACKOFF_SECONDS = 0.5
_RECONNECT_MAX_BACKOFF_SECONDS = 8.0
_PROTOCOL = "opencode-server-http"
_PROMPT_MAX_CHARS = 60_000
_PROMPT_MAX_BYTES = 60_000
#: The server's public MessageID schema is msg_ + 12 hex time bytes + 14
#: base62 random chars; the id is the durable user-message identity upstream
#: echoes back as info.id and assistant messages reference as parentID.
_MESSAGE_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_MESSAGE_RANDOM_CHARS = 14


class OpenCodeServerRuntime(HarnessRuntime):
    """One participant's sessions on its own stock `opencode serve` process.

    Admission is exactly the probe-pinned 204 no-body answer plus the exact
    user-message id confirmed in API state or SSE; every post-write doubt is
    UNKNOWN and never replayed — the stock server duplicates a repeated
    messageID rather than deduplicating it.
    """

    def __init__(self, context: RuntimeContext) -> None:
        if context.endpoint is None:
            raise ValueError(
                "the OpenCode server runtime requires the endpoint the backend "
                "announced on stdout; a discovered endpoint must be persisted "
                "before the runtime connects"
            )
        if context.token_file is None:
            raise ValueError(
                "the OpenCode server runtime requires the core-minted runtime "
                "credential; a declared credential must exist before the runtime "
                "connects"
            )
        self.context = context
        self._client = OpenCodeClient(endpoint=context.endpoint, token_file=context.token_file)
        self._source = OpenCodeServerLiveSource()
        self._session_id: str | None = None
        self._events_task: asyncio.Task[None] | None = None
        self._message_counter = 0
        self._closed = False

    async def open_session(
        self,
        *,
        mode: SessionOpenMode,
        native_session_id: str | None = None,
    ) -> RuntimeBinding:
        if self._closed:
            raise RuntimeError("the OpenCode server runtime is closed")
        version = await self._client.health()
        if mode is SessionOpenMode.NEW:
            session_id = await self._client.create_session()
            # A session Theater just created cannot have a turn in flight.
            await self._confirm_readback(session_id)
            self._source.adopt(session_id, RuntimeExecutionState.IDLE)
        elif mode is SessionOpenMode.FORK:
            if native_session_id is None:
                raise ValueError("forking requires the parent OpenCode session id")
            session_id = await self._client.fork_session(native_session_id)
            # A fork is a fresh session; its own turn cannot be running yet.
            await self._confirm_readback(session_id)
            self._source.adopt(session_id, RuntimeExecutionState.IDLE)
        elif mode is SessionOpenMode.RECONNECT:
            if native_session_id is None:
                raise ValueError("reconnecting requires the exact OpenCode session id")
            # Subscribe before readback so no transition event is missed; the
            # stock server replays no state to a fresh subscription.
            self._source.adopt(native_session_id, RuntimeExecutionState.UNKNOWN)
            self._start_events()
            await self._confirm_readback(native_session_id)
            session_id = native_session_id
        else:
            raise ValueError(f"unsupported OpenCode session open mode: {mode}")
        if mode is not SessionOpenMode.RECONNECT:
            self._start_events()
        self._session_id = session_id
        return RuntimeBinding(
            participant_id=self.context.participant_id,
            backend_generation=self.context.backend_generation,
            wiring=RuntimeWiring.NATIVE,
            lifecycle=RuntimeLifecyclePhase.BOUND,
            endpoint=self.context.endpoint,
            native_session_id=session_id,
            protocol=_PROTOCOL,
            native_version=version,
            compatibility_policy=OPENCODE_SERVER_COMPATIBILITY_POLICY,
        )

    async def frontend_plan(self, *, native_session_id: str | None = None) -> LaunchPlan:
        if not isinstance(native_session_id, str) or not native_session_id.strip():
            raise ValueError(
                "the session-first server topology requires the exact OpenCode "
                "session id before the attach plan is built"
            )
        assert self.context.endpoint is not None and self.context.token_file is not None
        return LaunchPlan(
            argv=["opencode", "attach", self.context.endpoint, "--session", native_session_id],
            secret_env={SERVER_SECRET_ENV: Path(self.context.token_file)},
        )

    def live_source(self) -> Source:
        self._start_events()
        return self._source

    async def snapshot(self) -> RuntimeSnapshot:
        self._start_events()
        health = self._source.connection_health
        capabilities = self._capabilities(health)
        return RuntimeSnapshot(
            participant_id=self.context.participant_id,
            backend_generation=self.context.backend_generation,
            native_session_id=self._session_id,
            native_turn_id=self._source.active_message_id() if self._session_id else None,
            settings=RuntimeSettings(),
            capabilities=capabilities,
            health=health,
            health_diagnostics=()
            if health is ConnectionHealth.CONNECTED
            else ("the OpenCode server event stream is not connected",),
            execution_state=self._source.current_execution_state(),
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
        if self._session_id is None:
            return _receipt(
                operation_id,
                DeliveryResult.REJECTED,
                error_code="not_ready",
                error="the OpenCode server runtime has no open session",
            )
        if (
            self._source.connection_health is not ConnectionHealth.CONNECTED
            and not await self._reconcile()
        ):
            return _receipt(
                operation_id,
                DeliveryResult.REJECTED,
                error_code="not_ready",
                error=(
                    "the OpenCode server connection was re-read and is not "
                    "available; the prompt was not submitted"
                ),
            )
        state = self._source.current_execution_state()
        if state is RuntimeExecutionState.ACTIVE:
            return _receipt(
                operation_id,
                DeliveryResult.REJECTED,
                error_code="busy",
                error="an OpenCode turn is already active; Theater queues the input instead",
            )
        if state is not RuntimeExecutionState.IDLE:
            return _receipt(
                operation_id,
                DeliveryResult.REJECTED,
                error_code="not_ready",
                error=("the session's idle state is not proven; Theater queues the input instead"),
            )
        message_id = self._mint_message_id()
        confirmation = self._source.register_confirmation(message_id)
        body = {"messageID": message_id, "parts": [{"type": "text", "text": prompt}]}
        try:
            result = await self._client.prompt_async(self._session_id, body)
        except asyncio.CancelledError:
            self._source.discard_confirmation(message_id)
            raise
        except OpenCodeHttpError as exc:
            self._source.discard_confirmation(message_id)
            return self._map_prompt_error(operation_id, exc)
        except Exception as exc:
            self._source.discard_confirmation(message_id)
            return _unknown(operation_id, f"the prompt request failed in an unmapped way ({exc})")
        if result is not None:
            self._source.discard_confirmation(message_id)
            return _unknown(
                operation_id,
                "prompt_async answered with a body; the qualified release answers 204 with no body",
            )
        # The 204 admits the turn: it owns the session until session.idle.
        self._source.note_submitted(message_id)
        if not await self._await_confirmation(message_id, confirmation):
            return _unknown(
                operation_id,
                "the exact user message was not confirmed in API state or SSE",
            )
        return _receipt(operation_id, DeliveryResult.ACCEPTED, native_turn_id=message_id)

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
            "steering an OpenCode server turn has no public surface; queue a follow-up instead",
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
            "the OpenCode server runtime has no confirmed settings surface",
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._source.disconnected()
        if self._events_task is not None:
            self._events_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._events_task

    # ---- internals ----------------------------------------------------

    def _capabilities(self, health: ConnectionHealth) -> RuntimeCapabilities:
        reasons: dict[RuntimeCapability, CapabilityUnavailableReason] = dict.fromkeys(
            (
                RuntimeCapability.INTERRUPT,
                RuntimeCapability.STEER,
                RuntimeCapability.SETTINGS_UPDATE,
            ),
            CapabilityUnavailableReason.THEATER_POLICY,
        )
        available: frozenset[RuntimeCapability] = frozenset()
        if health is ConnectionHealth.CONNECTED and self._session_id is not None:
            available = frozenset((RuntimeCapability.SEND, RuntimeCapability.QUEUE_FOLLOWUP))
        else:
            reason = (
                CapabilityUnavailableReason.GATED_BY_BACKEND
                if health in {ConnectionHealth.UNOPENED, ConnectionHealth.DISCONNECTED}
                else CapabilityUnavailableReason.SESSION_STATE
            )
            reasons[RuntimeCapability.SEND] = reason
            reasons[RuntimeCapability.QUEUE_FOLLOWUP] = reason
        return RuntimeCapabilities(available=available, unavailable_reasons=reasons)

    async def _confirm_readback(self, session_id: str) -> None:
        readback = await self._client.read_session(session_id)
        if readback.get("id") != session_id:
            raise RuntimeError(
                "the OpenCode server read back a different session id than requested; "
                "refusing to adopt it"
            )

    async def _reconcile(self) -> bool:
        """Health plus exact readback; the only proof reconnect may trust."""
        if self._closed or self._session_id is None:
            return False
        try:
            await self._client.health()
            readback = await self._client.read_session(self._session_id)
        except Exception:
            return False
        if readback.get("id") != self._session_id:
            return False
        self._source.reconcile_succeeded()
        return True

    async def _await_confirmation(
        self, message_id: str, confirmation: asyncio.Future[None]
    ) -> bool:
        try:
            await asyncio.wait_for(confirmation, timeout=_CONFIRM_DEADLINE_SECONDS)
        except TimeoutError:
            pass
        else:
            return True
        # The SSE stream may be quiet while the durable store is not: one
        # bounded readback decides between admission proof and UNKNOWN.
        try:
            messages = await self._client.list_messages(self._session_id or "")
        except Exception:
            return False
        for message in messages:
            info = message.get("info")
            if isinstance(info, dict) and info.get("id") == message_id:
                return True
        return False

    def _map_prompt_error(self, operation_id: str, exc: OpenCodeHttpError) -> ControlReceipt:
        if not exc.written:
            return _receipt(
                operation_id,
                DeliveryResult.REJECTED,
                error_code="server_refused",
                error=f"the OpenCode server refused the prompt before delivery ({exc})",
            )
        status = exc.status
        if status is not None and 400 <= status < 500:
            code = "invalid_session" if status == 404 else "server_refused"
            return _receipt(
                operation_id,
                DeliveryResult.REJECTED,
                error_code=code,
                error=f"the OpenCode server refused the prompt with HTTP {status} ({exc.reason})",
            )
        return _unknown(operation_id, str(exc))

    def _mint_message_id(self) -> str:
        self._message_counter = (self._message_counter + 1) % 0x1000
        value = int(time.time() * 1000) * 0x1000 + self._message_counter
        head = (value & 0xFFFF_FFFF_FFFF).to_bytes(6, "big").hex()
        tail = "".join(secrets.choice(_MESSAGE_ALPHABET) for _ in range(_MESSAGE_RANDOM_CHARS))
        return f"msg_{head}{tail}"

    def _start_events(self) -> None:
        if self._events_task is not None or self._closed:
            return
        self._events_task = asyncio.get_running_loop().create_task(self._run_events())

    async def _run_events(self) -> None:
        backoff = _RECONNECT_BACKOFF_SECONDS
        while not self._closed:
            try:
                self._source.connected()
                async for event in self._client.events():
                    self._source.feed(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            self._source.stream_lost()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _RECONNECT_MAX_BACKOFF_SECONDS)
            if await self._reconcile():
                backoff = _RECONNECT_BACKOFF_SECONDS


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


__all__ = ["OpenCodeServerRuntime", "opencode_server_runtime_factory"]


def opencode_server_runtime_factory(context: RuntimeContext) -> HarnessRuntime:
    return OpenCodeServerRuntime(context)
