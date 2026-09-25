"""Codex native control operations and settings."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from theater.harness.contracts.events import clip
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ConnectionHealth,
    ControlReceipt,
    DeliveryResult,
    RuntimeCapabilities,
    RuntimeCapability,
    RuntimeConnectionClosed,
    RuntimeConnectionError,
    RuntimeRequestError,
    RuntimeRequestTimeout,
    RuntimeSettings,
)
from theater.models import Status

from ._runtime_host import CodexRuntimeHost
from .runtime_constants import (
    _CODEX_SETTING_FIELDS,
    CODEX_RUNTIME_CONTROL_TIMEOUT_SECONDS,
)
from .runtime_messages import (
    _bounded_str,
)


class CodexRuntimeControls(CodexRuntimeHost):
    _active_turn_id: str | None
    _thread_status: str | None
    _settings_available: bool | None
    _settings_gate_reason: CapabilityUnavailableReason | None
    _status_hint: Status | None

    async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
        session = self._require_session()
        params: dict[str, object] = {
            "threadId": session,
            "input": [{"type": "text", "text": prompt}],
            # Client message id for native correlation only; the operation id
            # itself is the durable Theater fact.
            "clientUserMessageId": operation_id,
        }
        try:
            try:
                result = await self._request("turn/start", params)
            except RuntimeRequestError as error:
                return self._rejected(operation_id, "turn_start_refused", error.message)
            except RuntimeRequestTimeout:
                return self._unknown_prompt_start(operation_id, "control_ack_timeout")
            except (RuntimeConnectionClosed, RuntimeConnectionError) as error:
                return self._unknown_prompt_start(operation_id, "connection_lost", str(error))
            turn = result.get("turn") if isinstance(result, Mapping) else None
            turn_id = _bounded_str(turn.get("id") if isinstance(turn, Mapping) else None, limit=512)
            if turn_id is None:
                # Accepted but uncorrelatable: never fabricate a turn identity.
                return self._unknown_prompt_start(operation_id, "malformed_turn_start_result")
            # The returned turn IS the turn to report: a simultaneous native-UI submission absorbs
            # this message into the already-active turn and the backend returns that same turn id.
            self._active_turn_id = turn_id
            self._thread_status = "active"
            await self._subscribe_after_rollout()
            return ControlReceipt(
                operation_id=operation_id,
                result=DeliveryResult.ACCEPTED,
                native_turn_id=turn_id,
            )
        except asyncio.CancelledError:
            # A cancellation can arrive after ``turn/start`` crossed the transport write — including
            # while waiting for subscription after an acknowledgement.
            self._unknown_prompt_start(operation_id, "control_cancelled")
            raise

    async def steer(
        self,
        *,
        operation_id: str,
        native_turn_id: str,
        prompt: str,
    ) -> ControlReceipt:
        session = self._require_session()
        expected = _bounded_str(native_turn_id, limit=512)
        if expected is None:
            return self._rejected(operation_id, "stale_turn", "expectedTurnId is required")
        params: dict[str, object] = {
            "threadId": session,
            "expectedTurnId": expected,
            "input": [{"type": "text", "text": prompt}],
        }
        try:
            result = await self._request("turn/steer", params)
        except RuntimeRequestError as error:
            # A stale-turn refusal stays a refusal — never reinterpreted as a
            # send or a queued message.
            code = "stale_turn" if "no active turn" in error.message else "steer_refused"
            return self._rejected(operation_id, code, error.message)
        except RuntimeRequestTimeout:
            return self._unknown(operation_id, "control_ack_timeout")
        except (RuntimeConnectionClosed, RuntimeConnectionError) as error:
            return self._unknown(operation_id, "connection_lost", str(error))
        steered = _bounded_str(
            result.get("turnId") if isinstance(result, Mapping) else None, limit=512
        )
        if steered is None:
            return self._unknown(operation_id, "malformed_turn_steer_result")
        if steered != expected:
            # The verified dialect echoes the steered active turn; any other id is an
            # identity contradiction, never an acceptance.
            return self._unknown(operation_id, "turn_identity_mismatch")
        return ControlReceipt(
            operation_id=operation_id,
            result=DeliveryResult.ACCEPTED,
            native_turn_id=steered,
        )

    async def interrupt(
        self,
        *,
        operation_id: str,
        native_turn_id: str | None = None,
    ) -> ControlReceipt:
        session = self._require_session()
        turn_id = _bounded_str(native_turn_id, limit=512) or self._active_turn_id
        if turn_id is None:
            return self._rejected(
                operation_id,
                "no_active_turn",
                "no active native turn to interrupt on this thread",
            )
        try:
            await self._request("turn/interrupt", {"threadId": session, "turnId": turn_id})
        except RuntimeRequestError as error:
            return self._rejected(operation_id, "interrupt_refused", error.message)
        except RuntimeRequestTimeout:
            return self._unknown(operation_id, "control_ack_timeout")
        except (RuntimeConnectionClosed, RuntimeConnectionError) as error:
            return self._unknown(operation_id, "connection_lost", str(error))
        return ControlReceipt(
            operation_id=operation_id,
            result=DeliveryResult.ACCEPTED,
            native_turn_id=turn_id,
        )

    async def update_settings(
        self,
        *,
        operation_id: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ControlReceipt:
        session = self._require_session()
        if model is None and reasoning_effort is None:
            return self._rejected(
                operation_id, "no_settings_fields", "supply model and/or reasoning_effort"
            )
        if self._thread_status == "active" or self._active_turn_id is not None:
            # Idle-only, rechecked at dispatch: a busy thread never changes settings.
            return self._rejected(operation_id, "session_busy", "settings updates are idle-only")
        if self._settings_available is False:
            return self._rejected(
                operation_id,
                "settings_unavailable",
                f"thread/settings/update is unavailable on this backend: "
                f"{self._settings_gate_reason or CapabilityUnavailableReason.GATED_BY_BACKEND}",
            )
        params: dict[str, object] = {"threadId": session}
        if model is not None:
            params["model"] = model
        if reasoning_effort is not None:
            params["effort"] = reasoning_effort
        try:
            await self._request("thread/settings/update", params)
            self._settings_available = True
            self._settings_gate_reason = None
        except RuntimeRequestError as error:
            self._mark_settings_gate(error)
            return self._rejected(
                operation_id,
                "settings_unavailable",
                f"thread/settings/update refused: {error.message}",
            )
        except RuntimeRequestTimeout:
            return self._unknown(operation_id, "control_ack_timeout")
        except (RuntimeConnectionClosed, RuntimeConnectionError) as error:
            return self._unknown(operation_id, "connection_lost", str(error))
        unconfirmed = await self._readback_settings(
            session, want_model=model, want_effort=reasoning_effort
        )
        if unconfirmed is not None:
            # A readback that omits or contradicts a requested field is never accepted;
            # delivery stays UNKNOWN and a readable readback's values are adopted.
            self._degrade("settings update accepted but unconfirmed by native readback")
            code = (
                "settings_contradicted" if unconfirmed == "contradicted" else "settings_unconfirmed"
            )
            return self._unknown(
                operation_id,
                code,
                "backend accepted thread/settings/update but native readback did not "
                f"confirm the requested settings ({unconfirmed})",
            )
        return ControlReceipt(operation_id=operation_id, result=DeliveryResult.ACCEPTED)

    async def _probe_settings_gate(self) -> None:
        """Honestly determine the experimental settings gate, idle-only."""
        if self._thread_status == "active" or self._active_turn_id is not None:
            return
        session = self._native_session_id
        if session is None or self._connection is None:
            return
        try:
            await self._connection.request(
                "thread/settings/update",
                {"threadId": session},
                timeout=CODEX_RUNTIME_CONTROL_TIMEOUT_SECONDS,
            )
        except RuntimeRequestError as error:
            self._mark_settings_gate(error)
            return
        except (RuntimeRequestTimeout, RuntimeConnectionClosed, RuntimeConnectionError) as error:
            self._diagnostic(f"settings gate undetermined: {error}")
            return
        self._settings_available = True
        self._settings_gate_reason = None

    def _mark_settings_gate(self, error: RuntimeRequestError) -> None:
        self._settings_available = False
        self._settings_gate_reason = (
            CapabilityUnavailableReason.GATED_BY_BACKEND
            if error.code == -32600 or "experimentalApi" in error.message
            else CapabilityUnavailableReason.NOT_DETERMINED
        )

    async def _readback_settings(
        self, session: str, *, want_model: str | None, want_effort: str | None
    ) -> str | None:
        """Return why readback failed, or None after adopting confirmed values."""
        try:
            result = await self._request(
                "thread/read", {"threadId": session, "includeTurns": False}
            )
        except (RuntimeRequestError, RuntimeRequestTimeout, RuntimeConnectionError):
            self._diagnostic("settings readback unavailable; application stays uncertain")
            return "unreadable"
        thread = result.get("thread") if isinstance(result, Mapping) else result
        if not isinstance(thread, Mapping):
            self._diagnostic("settings readback carried no thread; application stays uncertain")
            return "unreadable"
        self._adopt_thread_settings(thread)
        model = _bounded_str(thread.get("model"), limit=512)
        effort = _bounded_str(thread.get("reasoningEffort") or thread.get("effort"), limit=512)
        if want_model is not None and model != want_model:
            self._diagnostic(
                f"settings readback did not reflect model {want_model!r} (reported {model!r})"
            )
            return "contradicted" if model is not None else "missing"
        if want_effort is not None and effort != want_effort:
            self._diagnostic(
                f"settings readback did not reflect effort {want_effort!r} (reported {effort!r})"
            )
            return "contradicted" if effort is not None else "missing"
        return None

    def _adopt_thread_settings(self, thread: Mapping[str, object]) -> bool:
        model = _bounded_str(thread.get("model"), limit=512)
        effort = _bounded_str(thread.get("reasoningEffort") or thread.get("effort"), limit=512)
        if model is None and effort is None:
            return False
        self._settings = RuntimeSettings(
            model=model,
            reasoning_effort=effort,
            supported_fields=_CODEX_SETTING_FIELDS,
        )
        return True

    def _capabilities(self) -> RuntimeCapabilities:
        available: set[RuntimeCapability] = set()
        unavailable: dict[RuntimeCapability, CapabilityUnavailableReason] = {
            RuntimeCapability.QUEUE_FOLLOWUP: CapabilityUnavailableReason.THEATER_POLICY,
        }
        bound = self._native_session_id is not None
        if bound:
            available.add(RuntimeCapability.SEND)
            available.add(RuntimeCapability.INTERRUPT)
        else:
            unavailable[RuntimeCapability.SEND] = CapabilityUnavailableReason.SESSION_STATE
            unavailable[RuntimeCapability.INTERRUPT] = CapabilityUnavailableReason.SESSION_STATE
        if self._active_turn_id is not None:
            available.add(RuntimeCapability.STEER)
        else:
            unavailable[RuntimeCapability.STEER] = CapabilityUnavailableReason.SESSION_STATE
        if self._settings_available is True:
            available.add(RuntimeCapability.SETTINGS_UPDATE)
        else:
            unavailable[RuntimeCapability.SETTINGS_UPDATE] = (
                self._settings_gate_reason
                if self._settings_gate_reason is not None
                else CapabilityUnavailableReason.NOT_DETERMINED
            )
        return RuntimeCapabilities(available=frozenset(available), unavailable_reasons=unavailable)

    def _rejected(self, operation_id: str, code: str, message: str) -> ControlReceipt:
        return ControlReceipt(
            operation_id=operation_id,
            result=DeliveryResult.REJECTED,
            error_code=code,
            error=clip(message) or None,
        )

    def _unknown(self, operation_id: str, code: str, detail: str | None = None) -> ControlReceipt:
        # UNKNOWN delivery: no retry, no tmux fallback; reconciliation only.
        message = f"{code}: native transmission or acceptance was uncertain"
        if detail:
            message = f"{message} ({detail})"
        self._diagnostic(message)
        return ControlReceipt(
            operation_id=operation_id,
            result=DeliveryResult.UNKNOWN,
            error_code=code,
            error=clip(message) or None,
        )

    def _unknown_prompt_start(
        self, operation_id: str, code: str, detail: str | None = None
    ) -> ControlReceipt:
        """Fail closed before returning an ambiguous prompt-start receipt."""
        self._active_turn_id = None
        self._thread_status = None
        self._status_hint = None
        self._subscribed = False
        self._health = ConnectionHealth.DISCONNECTED
        self._diagnostic(f"{code}: ambiguous turn/start invalidated cached native session state")
        # The observer's existing activity hook wakes both its status reader and the daemon
        # composition's recovery path.
        self._notify_activity()
        self._health = ConnectionHealth.DISCONNECTED
        return self._unknown(operation_id, code, detail)
