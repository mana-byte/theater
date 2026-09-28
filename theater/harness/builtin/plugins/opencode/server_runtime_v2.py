"""Detached OpenCode 2.x server runtime: exact sessions and native send over HTTP.

The session exists before the TUI attaches with `--server <url> -s <id>`, so the initial prompt
goes through `send`, never the TUI's `--prompt` auto-submit (racy on a cold 2.0.18 server).
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from theater import paths
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

from . import ids_v2
from .approval_v2 import require_plugin_active
from .dialect import resolve_binary
from .http import OpenCodeHttpError
from .http_v2 import OpenCodeV2Client, model_ref
from .launch import tui_env_v2
from .native_plugin_v2 import PLUGIN_ID, plugin_dir
from .runtime_plan import (
    OPENCODE_SERVER_V2_COMPATIBILITY_POLICY,
    OPENCODE_SERVER_V2_MAX_VERSION,
    OPENCODE_SERVER_V2_MIN_VERSION,
    parse_opencode_version,
)
from .server_live_v2 import OpenCodeV2LiveSource
from .server_plan import SERVER_SECRET_ENV

_PROTOCOL = "opencode-server-http-v2"
_POLL_SECONDS = 0.5
_PROMPT_MAX_CHARS = 60_000
_PROMPT_MAX_BYTES = 60_000


class OpenCodeServerV2Runtime(HarnessRuntime):
    """One participant's session on its own stock 2.x `opencode serve` process."""

    def __init__(self, context: RuntimeContext) -> None:
        if context.endpoint is None or context.token_file is None:
            raise ValueError(
                "the OpenCode 2.x server runtime requires the announced endpoint and the "
                "core-minted runtime credential before it connects"
            )
        self.context = context
        self._client = OpenCodeV2Client(endpoint=context.endpoint, token_file=context.token_file)
        self._source = OpenCodeV2LiveSource()
        self._session_id: str | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._closed = False

    async def open_session(
        self,
        *,
        mode: SessionOpenMode,
        native_session_id: str | None = None,
    ) -> RuntimeBinding:
        if self._closed:
            raise RuntimeError("the OpenCode 2.x server runtime is closed")
        if mode is SessionOpenMode.FORK:
            # Refused before any pane exists, so the spawn resumes on the legacy route instead.
            raise RuntimeError(
                "an OpenCode 2.x server opens only its own participant's database, and the "
                "parent session lives in its lineage's; the resume continues on the legacy route"
            )
        version = await self._verified_version()
        state = RuntimeExecutionState.IDLE
        if mode is SessionOpenMode.NEW:
            model = model_ref(self.context.model) if self.context.model else None
            session_id = await self._client.create_session(directory=self.context.cwd, model=model)
        elif mode is SessionOpenMode.RECONNECT:
            if native_session_id is None:
                raise ValueError("reconnecting requires the exact OpenCode session id")
            session_id = native_session_id
            state = RuntimeExecutionState.UNKNOWN
        else:
            raise ValueError(f"unsupported OpenCode session open mode: {mode}")
        readback = await self._client.read_session(session_id)
        if readback.get("id") != session_id:
            raise RuntimeError(
                "the OpenCode server read back a different session id than requested; "
                "refusing to adopt it"
            )
        # Fail closed: core swallows a plugin load failure, so the exact generated
        # plugin must be proven active on this server before any UI or send access.
        await require_plugin_active(
            self._client,
            plugin_id=PLUGIN_ID,
            source_path=self._plugin_source(),
            directory=self.context.cwd,
        )
        self._session_id = session_id
        self._source.adopt(session_id, state)
        await self._poll_once()
        self._start_polling()
        return RuntimeBinding(
            participant_id=self.context.participant_id,
            backend_generation=self.context.backend_generation,
            wiring=RuntimeWiring.NATIVE,
            lifecycle=RuntimeLifecyclePhase.BOUND,
            endpoint=self.context.endpoint,
            native_session_id=session_id,
            protocol=_PROTOCOL,
            native_version=version,
            compatibility_policy=OPENCODE_SERVER_V2_COMPATIBILITY_POLICY,
        )

    async def frontend_plan(self, *, native_session_id: str | None = None) -> LaunchPlan:
        if not isinstance(native_session_id, str) or not native_session_id.strip():
            raise ValueError(
                "the session-first server topology requires the exact OpenCode "
                "session id before the attach plan is built"
            )
        assert self.context.endpoint is not None and self.context.token_file is not None
        binary = resolve_binary(self.context.binary or "opencode")
        argv = [binary, "--server", self.context.endpoint, "-s", native_session_id]
        if self.context.approval == "yolo":
            argv.append("--auto")
        return LaunchPlan(
            argv=argv,
            env=tui_env_v2(self.context.approval),
            secret_env={SERVER_SECRET_ENV: Path(self.context.token_file)},
        )

    def live_source(self) -> Source:
        self._start_polling()
        return self._source

    async def snapshot(self) -> RuntimeSnapshot:
        self._start_polling()
        health = self._source.connection_health
        return RuntimeSnapshot(
            participant_id=self.context.participant_id,
            backend_generation=self.context.backend_generation,
            native_session_id=self._session_id,
            native_turn_id=None,
            settings=RuntimeSettings(),
            capabilities=self._capabilities(health),
            health=health,
            health_diagnostics=()
            if health is ConnectionHealth.CONNECTED
            else ("the OpenCode 2.x server is not answering session.active",),
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
        if self._source.connection_health is not ConnectionHealth.CONNECTED:
            await self._poll_once()
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
                error="the session's idle state is not proven; Theater queues the input instead",
            )
        message_id = ids_v2.message_id()
        try:
            # `queue`, not the default `steer`: a turn a human started meanwhile is not amended.
            admitted = await self._client.prompt(
                self._session_id, message_id=message_id, text=prompt, delivery="queue"
            )
        except OpenCodeHttpError as exc:
            return _map_http_error(operation_id, exc, "prompt")
        except Exception as exc:
            return _unknown(operation_id, f"the prompt request failed in an unmapped way ({exc})")
        if admitted.get("id") != message_id:
            return _unknown(operation_id, "the server admitted a different message than was sent")
        self._source.note_submitted()
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
            "a 2.x steer has no turn identity to pin and lands as a new prompt once the turn "
            "ends; queue a follow-up instead",
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
            "the manifest routes OpenCode interrupts through the terminal for every release",
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
            operation_id, "the OpenCode 2.x server runtime has no confirmed settings surface"
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._source.disconnected()
        task, self._poll_task = self._poll_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

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
        controls = (RuntimeCapability.SEND, RuntimeCapability.QUEUE_FOLLOWUP)
        if health is ConnectionHealth.CONNECTED and self._session_id is not None:
            return RuntimeCapabilities(available=frozenset(controls), unavailable_reasons=reasons)
        reason = (
            CapabilityUnavailableReason.GATED_BY_BACKEND
            if health in {ConnectionHealth.UNOPENED, ConnectionHealth.DISCONNECTED}
            else CapabilityUnavailableReason.SESSION_STATE
        )
        reasons.update(dict.fromkeys(controls, reason))
        return RuntimeCapabilities(available=frozenset(), unavailable_reasons=reasons)

    def _plugin_source(self) -> str:
        """The exact generated plugin entrypoint this participant's plan rendered."""
        config_path = self.context.config_path or paths.mcp_config_path(self.context.participant_id)
        return str((plugin_dir(config_path) / "server.js").resolve())

    async def _verified_version(self) -> str:
        version = await self._client.version()
        parsed = parse_opencode_version(version)
        if parsed is None or not (
            OPENCODE_SERVER_V2_MIN_VERSION <= parsed < OPENCODE_SERVER_V2_MAX_VERSION
        ):
            raise RuntimeError(
                f"OpenCode server {version!r} is outside the qualified 2.x server range"
            )
        return version

    async def _poll_once(self) -> None:
        try:
            active = await self._client.active()
        except Exception:
            self._source.observe(None)
            return
        self._source.observe(active)

    def _start_polling(self) -> None:
        if self._poll_task is not None or self._closed or self._session_id is None:
            return
        self._poll_task = asyncio.get_running_loop().create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        while not self._closed:
            await self._poll_once()
            await asyncio.sleep(_POLL_SECONDS)


def _map_http_error(operation_id: str, exc: OpenCodeHttpError, what: str) -> ControlReceipt:
    if not exc.written:
        return _receipt(
            operation_id,
            DeliveryResult.REJECTED,
            error_code="server_refused",
            error=f"the OpenCode server refused the {what} before delivery ({exc})",
        )
    status = exc.status
    if status is not None and 400 <= status < 500:
        code = "invalid_session" if status == 404 else "server_refused"
        return _receipt(
            operation_id,
            DeliveryResult.REJECTED,
            error_code=code,
            error=f"the OpenCode server refused the {what} with HTTP {status} ({exc.reason})",
        )
    return _unknown(operation_id, str(exc))


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
            f"the OpenCode delivery became uncertain and Theater did not replay it ({detail[:256]})"
        ),
    )


def _gated(operation_id: str, detail: str) -> ControlReceipt:
    return _receipt(
        operation_id, DeliveryResult.REJECTED, error_code="theater_policy", error=detail
    )


__all__ = ["OpenCodeServerV2Runtime"]
