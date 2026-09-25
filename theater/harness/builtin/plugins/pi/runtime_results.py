"""Pi frontend control-result validation and receipts."""

from __future__ import annotations

from collections.abc import Mapping

from theater.harness.contracts.runtime import (
    ControlReceipt,
    DeliveryResult,
    RuntimeRequestError,
)

from ._runtime_host import PiFrontendRuntimeHost
from .runtime_protocol import (
    PiFrontendProtocolError,
    _bounded_string,
    _decode_snapshot,
)


class PiFrontendRuntimeResults(PiFrontendRuntimeHost):
    def _decode_interrupt_result(
        self,
        value: object,
        *,
        expected_operation_id: str,
        expected_session_id: str,
        expected_turn_id: str,
        expected_bridge_epoch: int,
    ) -> dict[str, str]:
        """Require the accepted echo to name the exact aborted run and epoch."""
        if not isinstance(value, Mapping):
            raise PiFrontendProtocolError("Pi interrupt response must be an object")
        if value.get("status") != "accepted":
            raise PiFrontendProtocolError("Pi interrupt response was not accepted")
        operation_id = _bounded_string(value.get("operation_id"), "interrupt operation id")
        session_id = _bounded_string(value.get("native_session_id"), "interrupt session id")
        turn_id = _bounded_string(value.get("native_turn_id"), "interrupt native turn id")
        if operation_id != expected_operation_id:
            raise PiFrontendProtocolError("Pi interrupt echoed a different operation")
        if session_id != expected_session_id:
            raise PiFrontendProtocolError("Pi interrupt echoed a different native session")
        if turn_id != expected_turn_id:
            raise PiFrontendProtocolError("Pi interrupt echoed a different native turn")
        snapshot_data = {
            "protocol": value.get("protocol"),
            "native_session_id": session_id,
            "native_turn_id": value.get("native_turn_id"),
            "bridge_epoch": value.get("bridge_epoch"),
            "snapshot_revision": value.get("snapshot_revision"),
            "sequence": value.get("sequence"),
            "settings": value.get("settings"),
            "execution_state": value.get("execution_state"),
            "pending_interaction": value.get("pending_interaction"),
            "capabilities": value.get("capabilities"),
        }
        snapshot = _decode_snapshot(snapshot_data)
        if snapshot.bridge_epoch != expected_bridge_epoch:
            raise PiFrontendProtocolError("Pi interrupt echoed a different bridge epoch")
        return {"operation_id": operation_id, "native_turn_id": turn_id}

    def _decode_send_result(self, value: object) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise PiFrontendProtocolError("Pi send response must be an object")
        if value.get("status") != "accepted":
            raise PiFrontendProtocolError("Pi send response was not accepted")
        operation_id = _bounded_string(value.get("operation_id"), "send operation id")
        session_id = _bounded_string(value.get("native_session_id"), "send native session id")
        turn_id = _bounded_string(value.get("native_turn_id"), "send native turn id")
        snapshot_data = {
            "protocol": value.get("protocol"),
            "native_session_id": session_id,
            "native_turn_id": value.get("native_turn_id"),
            "bridge_epoch": value.get("bridge_epoch"),
            "snapshot_revision": value.get("snapshot_revision"),
            "sequence": value.get("sequence"),
            "settings": value.get("settings"),
            "execution_state": value.get("execution_state"),
            "pending_interaction": value.get("pending_interaction"),
            "capabilities": value.get("capabilities"),
        }
        snapshot = _decode_snapshot(snapshot_data)
        # The readback may honestly report no active turn; the receipt id is
        # the identity.
        if not self._apply_snapshot(snapshot):
            raise PiFrontendProtocolError("Pi send snapshot conflicted with live identity")
        return {
            "operation_id": operation_id,
            "native_session_id": session_id,
            "native_turn_id": turn_id,
        }

    def _decode_settings_result(self, value: object) -> dict[str, object]:
        if not isinstance(value, Mapping):
            raise PiFrontendProtocolError("Pi settings response must be an object")
        if value.get("status") != "accepted":
            raise PiFrontendProtocolError("Pi settings response was not accepted")
        operation_id = _bounded_string(value.get("operation_id"), "settings operation id")
        session_id = _bounded_string(value.get("native_session_id"), "settings native session id")
        snapshot_data = {
            "protocol": value.get("protocol"),
            "native_session_id": session_id,
            "bridge_epoch": value.get("bridge_epoch"),
            "snapshot_revision": value.get("snapshot_revision"),
            "sequence": value.get("sequence"),
            "settings": value.get("settings"),
            "execution_state": value.get("execution_state"),
            "pending_interaction": value.get("pending_interaction"),
            "capabilities": value.get("capabilities"),
        }
        return {
            "operation_id": operation_id,
            "native_session_id": session_id,
            "snapshot": _decode_snapshot(snapshot_data),
        }

    def _request_error_receipt(
        self, operation_id: str, exc: RuntimeRequestError, rejected: frozenset[str]
    ) -> ControlReceipt:
        """Only a bounded pre-mutation code is REJECTED; the rest stays UNKNOWN."""
        code: str | None = None
        if isinstance(exc.code, str):
            code = exc.code
        if code is None:
            return self._unknown(operation_id, str(exc.code), exc.message)
        if code in rejected:
            return self._rejected(operation_id, code, exc.message)
        return self._unknown(operation_id, code, exc.message)

    def _proof_gated(self, operation_id: str, control: str) -> ControlReceipt:
        return self._rejected(
            operation_id,
            "native_control_proof_gated",
            f"Pi native {control} remains disabled pending public lifecycle conformance proof",
        )

    @staticmethod
    def _rejected(operation_id: str, code: str, message: str) -> ControlReceipt:
        return ControlReceipt(
            operation_id=operation_id,
            result=DeliveryResult.REJECTED,
            error_code=code,
            error=message,
        )

    @staticmethod
    def _unknown(operation_id: str, code: str, message: str) -> ControlReceipt:
        return ControlReceipt(
            operation_id=operation_id,
            result=DeliveryResult.UNKNOWN,
            error_code=code,
            error=message,
        )
