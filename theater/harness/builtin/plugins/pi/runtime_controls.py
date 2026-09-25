"""Pi frontend native control operations."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

from theater.harness.contracts.runtime import (
    ConnectionHealth,
    ControlReceipt,
    DeliveryResult,
    RuntimeExecutionState,
    RuntimeRequestError,
)

from ._runtime_host import PiFrontendRuntimeHost
from .runtime_constants import (
    _REJECTED_INTERRUPT_ERRORS,
    _REJECTED_SEND_ERRORS,
    _REJECTED_SETTINGS_ERRORS,
    PI_FRONTEND_CONTROL_TIMEOUT_SECONDS,
    PI_FRONTEND_SEND_PROMPT_MAX_CHARS,
)
from .runtime_protocol import (
    PiFrontendPeer,
    PiFrontendProtocolError,
    _bounded_string,
    _FrontendSnapshot,
)


@dataclass(frozen=True, slots=True)
class _SettingsUpdateContext:
    peer: PiFrontendPeer
    session_id: str
    session_epoch: int
    bridge_epoch: int | None
    peer_generation: int


class PiFrontendRuntimeControls(PiFrontendRuntimeHost):
    async def update_settings(
        self,
        *,
        operation_id: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ControlReceipt:
        """Request one idle/session-guarded, confirmed Pi settings update.

        Any doubt is ``UNKNOWN``; nothing is replayed or substituted with a legacy control.
        """
        refusal = self._validate_settings_update(operation_id, model, reasoning_effort)
        if refusal is not None:
            return refusal
        prepared = await self._prepare_settings_update(operation_id)
        if isinstance(prepared, ControlReceipt):
            return prepared
        result = await self._request_settings_update(
            prepared,
            operation_id=operation_id,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        if isinstance(result, ControlReceipt):
            return result
        return self._confirm_settings_update(prepared, operation_id, result)

    def _validate_settings_update(
        self, operation_id: str, model: str | None, reasoning_effort: str | None
    ) -> ControlReceipt | None:
        if model is None and reasoning_effort is None:
            return self._rejected(operation_id, "invalid_request", "no Pi setting was supplied")
        # setModel awaits provider auth with no expected-session guard, so it stays
        # proof-gated: a model could otherwise land in a later human session.
        if model is not None:
            return self._rejected(
                operation_id,
                "model_update_proof_gated",
                "Pi model updates remain disabled pending an atomic public session guard",
            )
        if reasoning_effort is not None:
            try:
                _bounded_string(reasoning_effort, "requested reasoning effort")
            except PiFrontendProtocolError as exc:
                return self._rejected(operation_id, "invalid_request", str(exc))
        return None

    async def _prepare_settings_update(
        self, operation_id: str
    ) -> _SettingsUpdateContext | ControlReceipt:
        before = await self.snapshot()
        session_id = before.native_session_id
        if before.health is not ConnectionHealth.CONNECTED or session_id is None:
            return self._rejected(
                operation_id,
                "native_settings_unavailable",
                "Pi frontend settings are unavailable while its bridge is disconnected",
            )
        if before.execution_state is not RuntimeExecutionState.IDLE:
            return self._rejected(
                operation_id,
                "settings_not_idle",
                "Pi settings require a bridge-confirmed idle session",
            )

        peer = self._peer
        if peer is None:
            return self._rejected(
                operation_id,
                "native_settings_unavailable",
                "Pi frontend settings are unavailable while its bridge is disconnected",
            )
        return _SettingsUpdateContext(
            peer=peer,
            session_id=session_id,
            session_epoch=self._session_epoch,
            bridge_epoch=self._bridge_epoch,
            peer_generation=self._peer_generation,
        )

    async def _request_settings_update(
        self,
        prepared: _SettingsUpdateContext,
        *,
        operation_id: str,
        model: str | None,
        reasoning_effort: str | None,
    ) -> Mapping[str, object] | ControlReceipt:
        params: dict[str, object] = {
            "operation_id": operation_id,
            "native_session_id": prepared.session_id,
        }
        if model is not None:
            params["model"] = model
        if reasoning_effort is not None:
            params["reasoning_effort"] = reasoning_effort
        try:
            return await prepared.peer.request(
                "pi.settings.update", params, timeout=PI_FRONTEND_CONTROL_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            raise
        except RuntimeRequestError as exc:
            return self._request_error_receipt(operation_id, exc, _REJECTED_SETTINGS_ERRORS)
        except Exception as exc:
            self._mark_disconnected(f"pi.settings.update failed: {type(exc).__name__}: {exc}")
            return self._unknown(
                operation_id,
                "settings_delivery_unknown",
                "Pi settings delivery became uncertain; Theater did not replay it",
            )

    def _confirm_settings_update(
        self,
        prepared: _SettingsUpdateContext,
        operation_id: str,
        result: Mapping[str, object],
    ) -> ControlReceipt:
        try:
            confirmed = self._decode_settings_result(result)
        except PiFrontendProtocolError as exc:
            self._diagnostic(str(exc))
            self._health = ConnectionHealth.DEGRADED
            self._touch()
            return self._unknown(
                operation_id,
                "settings_unconfirmed",
                "Pi settings response was malformed; Theater did not replay it",
            )
        if (
            prepared.peer is not self._peer
            or prepared.peer_generation != self._peer_generation
            or prepared.session_epoch != self._session_epoch
            or prepared.bridge_epoch is None
            or confirmed["native_session_id"] != prepared.session_id
            or confirmed["operation_id"] != operation_id
            or not self._trusted_session_matches()
        ):
            return self._unknown(
                operation_id,
                "session_changed",
                "Pi session or bridge changed while settings were in flight",
            )

        snapshot = confirmed["snapshot"]
        assert isinstance(snapshot, _FrontendSnapshot)
        if snapshot.bridge_epoch != prepared.bridge_epoch or not self._apply_snapshot(snapshot):
            return self._unknown(
                operation_id,
                "session_changed",
                "Pi session or bridge changed before settings readback",
            )
        # Pi may clamp a thinking level.  The exact effective value is now in
        # ``snapshot().settings.reasoning_effort``; a clamp is confirmed, not
        # a fabricated success.
        return ControlReceipt(operation_id=operation_id, result=DeliveryResult.ACCEPTED)

    async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
        """Deliver one bridge-admitted prompt; post-delivery failures stay UNKNOWN."""
        if not isinstance(prompt, str) or not prompt.strip():
            return self._rejected(operation_id, "invalid_request", "Pi send requires a prompt")
        if len(prompt) > PI_FRONTEND_SEND_PROMPT_MAX_CHARS:
            return self._rejected(
                operation_id,
                "prompt_too_large",
                "Pi send prompt exceeds the bridge limit",
            )
        before = await self.snapshot()
        session_id = before.native_session_id
        if before.health is not ConnectionHealth.CONNECTED or session_id is None:
            return self._rejected(
                operation_id,
                "not_ready",
                "Pi frontend send is unavailable while its bridge is disconnected",
            )
        peer = self._peer
        if peer is None:
            return self._rejected(
                operation_id,
                "not_ready",
                "Pi frontend send is unavailable while its bridge is disconnected",
            )
        session_epoch = self._session_epoch
        params: dict[str, object] = {
            "operation_id": operation_id,
            "native_session_id": session_id,
            "prompt": prompt,
        }
        try:
            result = await peer.request(
                "pi.control.send", params, timeout=PI_FRONTEND_CONTROL_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            raise
        except RuntimeRequestError as exc:
            return self._request_error_receipt(operation_id, exc, _REJECTED_SEND_ERRORS)
        except Exception as exc:
            self._mark_disconnected(f"pi.control.send failed: {type(exc).__name__}: {exc}")
            return self._unknown(
                operation_id,
                "send_delivery_unknown",
                "Pi send delivery became uncertain; Theater did not replay it",
            )
        try:
            confirmed = self._decode_send_result(result)
        except PiFrontendProtocolError as exc:
            self._diagnostic(str(exc))
            self._health = ConnectionHealth.DEGRADED
            self._touch()
            return self._unknown(
                operation_id,
                "send_unconfirmed",
                "Pi send response was malformed; Theater did not replay it",
            )
        if (
            peer is not self._peer
            or session_epoch != self._session_epoch
            or confirmed["native_session_id"] != session_id
            or confirmed["operation_id"] != operation_id
            or not self._trusted_session_matches()
        ):
            return self._unknown(
                operation_id,
                "session_changed",
                "Pi session or bridge changed while the send turn was in flight",
            )
        return ControlReceipt(
            operation_id=operation_id,
            result=DeliveryResult.ACCEPTED,
            native_turn_id=confirmed["native_turn_id"],
        )

    async def steer(self, *, operation_id: str, native_turn_id: str, prompt: str) -> ControlReceipt:
        """Refuse unproven exact-turn steering without touching legacy send."""
        del native_turn_id, prompt
        return self._proof_gated(operation_id, "steer")

    async def interrupt(
        self, *, operation_id: str, native_turn_id: str | None = None
    ) -> ControlReceipt:
        """Interrupt one exact active turn; post-abort ambiguity stays UNKNOWN.

        The bridge checks scope atomically against live run state, never a stale snapshot.
        """
        if not isinstance(native_turn_id, str) or not native_turn_id.strip():
            return self._rejected(
                operation_id,
                "invalid_request",
                "Pi interrupt requires the expected active turn id",
            )
        expected_turn_id = native_turn_id
        session_id = self._native_session_id
        bridge_epoch = self._bridge_epoch
        peer = self._peer
        if (
            self._health is not ConnectionHealth.CONNECTED
            or peer is None
            or session_id is None
            or bridge_epoch is None
        ):
            return self._rejected(
                operation_id,
                "not_ready",
                "Pi frontend interrupt has no confirmed bridge identity",
            )
        session_epoch = self._session_epoch
        peer_generation = self._peer_generation
        params: dict[str, object] = {
            "operation_id": operation_id,
            "native_session_id": session_id,
            "expected_native_turn_id": expected_turn_id,
            "expected_bridge_epoch": bridge_epoch,
        }
        try:
            result = await peer.request(
                "pi.control.interrupt", params, timeout=PI_FRONTEND_CONTROL_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            raise
        except RuntimeRequestError as exc:
            return self._request_error_receipt(operation_id, exc, _REJECTED_INTERRUPT_ERRORS)
        except Exception as exc:
            self._mark_disconnected(f"pi.control.interrupt failed: {type(exc).__name__}: {exc}")
            return self._unknown(
                operation_id,
                "interrupt_unconfirmed",
                "Pi interrupt delivery became uncertain; Theater did not retry it",
            )
        try:
            confirmed = self._decode_interrupt_result(
                result,
                expected_operation_id=operation_id,
                expected_session_id=session_id,
                expected_turn_id=expected_turn_id,
                expected_bridge_epoch=bridge_epoch,
            )
        except PiFrontendProtocolError as exc:
            self._diagnostic(str(exc))
            self._health = ConnectionHealth.DEGRADED
            self._touch()
            return self._unknown(
                operation_id,
                "interrupt_unconfirmed",
                "Pi interrupt response was malformed; Theater did not retry it",
            )
        if (
            peer is not self._peer
            or peer_generation != self._peer_generation
            or session_epoch != self._session_epoch
            or not self._trusted_session_matches()
        ):
            return self._unknown(
                operation_id,
                "session_changed",
                "Pi session or bridge changed while the interrupt was in flight",
            )
        return ControlReceipt(
            operation_id=operation_id,
            result=DeliveryResult.ACCEPTED,
            native_turn_id=confirmed["native_turn_id"],
        )
