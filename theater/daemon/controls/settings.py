"""Settings updates: change an idle participant's model or reasoning effort."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

from theater.daemon.controls._common import (
    ACTION_SETTINGS_UPDATE,
    CONTROL_DELIVERY_ACCEPTED,
    CONTROL_DELIVERY_REJECTED,
    CONTROL_DELIVERY_UNKNOWN,
    CONTROL_UNKNOWN_ACK_LOST,
    CONTROL_UNKNOWN_READBACK_FAILED,
    CONTROL_UNKNOWN_RECEIPT_MISMATCH,
    CONTROL_UNKNOWN_RECEIPT_UNKNOWN,
    DELIVERY_UNKNOWN_ERROR_CODE,
    LABEL_SETTINGS_UPDATE,
    NativeControlPreparation,
    prepare_native_control,
)
from theater.daemon.controls._host import ControlHost
from theater.daemon.controls.busy import BusyOperation
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
    RuntimeCapability,
    RuntimeSettingField,
    RuntimeSnapshot,
)
from theater.models import BadRequest

logger = logging.getLogger("theater.daemon.controls")


@dataclass(frozen=True, slots=True)
class SettingsOutcome:
    """What one settings update established."""

    applied: bool | None
    model: str | None = None
    reasoning_effort: str | None = None
    error_code: str | None = None
    error: str | None = None


class SettingsControls(ControlHost):
    """Apply runtime settings updates."""

    async def update_settings(
        self,
        participant_id: str,
        *,
        caller_id: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        operation_id: str | None = None,
        on_reserved: Callable[[str, str | None], None] | None = None,
        pre_reserved: bool = False,
    ) -> SettingsOutcome:
        """Idle-only model/reasoning update, capability- and allowlist-gated."""
        with self._control_latency(ControlKind.SETTINGS_UPDATE, participant_id) as latency:
            outcome = await self._update_settings(
                participant_id,
                caller_id=caller_id,
                model=model,
                reasoning_effort=reasoning_effort,
                operation_id=operation_id,
                on_reserved=on_reserved,
                pre_reserved=pre_reserved,
            )
            # ``applied`` is True only after native confirmation/readback,
            # False on a definitive refusal, None while uncertain.
            if outcome.applied:
                latency.delivery = CONTROL_DELIVERY_ACCEPTED
            elif outcome.applied is not None:
                latency.delivery = CONTROL_DELIVERY_REJECTED
            else:
                latency.delivery = CONTROL_DELIVERY_UNKNOWN
            # A settings update can only complete over the native runtime;
            # the body established that fact, so no extra read is needed.
            latency.transport = ControlTransport.NATIVE_RUNTIME.value
            return outcome

    async def _update_settings(
        self,
        participant_id: str,
        *,
        caller_id: str,
        model: str | None,
        reasoning_effort: str | None,
        operation_id: str | None,
        on_reserved: Callable[[str, str | None], None] | None,
        pre_reserved: bool,
    ) -> SettingsOutcome:
        """The settings-update body."""
        if model is None and reasoning_effort is None:
            raise BadRequest(
                f"nothing to update for participant {participant_id!r}: supply "
                "model and/or reasoning_effort; approval and sandbox policy are "
                "never changed here"
            )
        runtime = self._runtime_for(participant_id)
        async with self._lock(participant_id):
            self._gates.authorize(participant_id, caller_id, ACTION_SETTINGS_UPDATE)
            await self._gates.require_absent(participant_id)
            self._gates.check_settings(model, reasoning_effort)
            route = self.route_for(participant_id, RuntimeCapability.SETTINGS_UPDATE)
            if not route.is_native:
                if not route.native_wiring:
                    raise BadRequest(
                        f"settings updates for participant {participant_id!r} require native "
                        "runtime wiring; its harness has no runtime, so the model is "
                        "fixed at launch"
                    )
                raise BadRequest(
                    f"settings updates for participant {participant_id!r} are unavailable on "
                    "its selected transport"
                )
            prepared = await prepare_native_control(
                self,
                runtime,
                NativeControlPreparation(
                    participant_id=participant_id,
                    route_capability=RuntimeCapability.SETTINGS_UPDATE,
                    required_capability=None,
                    action=LABEL_SETTINGS_UPDATE,
                    refusal_label=LABEL_SETTINGS_UPDATE,
                ),
            )
            snapshot = prepared.snapshot
            if not snapshot.capabilities.supports(RuntimeCapability.SETTINGS_UPDATE):
                reason = snapshot.capabilities.reason_for(RuntimeCapability.SETTINGS_UPDATE)
                raise BadRequest(
                    f"participant {participant_id!r} does not support settings "
                    f"updates ({reason}); the installed native API gates this "
                    "capability, so the model stays as configured at launch"
                )
            self._require_supported_settings(participant_id, snapshot, model, reasoning_effort)
            self._reject_busy(participant_id, snapshot, operation=BusyOperation.SETTINGS)
            payload = json.dumps(
                {
                    key: value
                    for key, value in (
                        ("model", model),
                        ("reasoning_effort", reasoning_effort),
                    )
                    if value is not None
                }
            )
            operation_id = operation_id or self._mint_operation_id(
                participant_id, ControlKind.SETTINGS_UPDATE
            )
            if pre_reserved:
                reserved = self._require_public_reservation(
                    operation_id,
                    participant_id=participant_id,
                    kind=ControlKind.SETTINGS_UPDATE,
                    phase=ControlDeliveryPhase.RESERVED,
                    route=prepared.route,
                )
                self._require_reserved_native_identity(reserved, snapshot)
            else:
                self._reserve(
                    operation_id,
                    participant_id=participant_id,
                    kind=ControlKind.SETTINGS_UPDATE,
                    transport=ControlTransport.NATIVE_RUNTIME,
                    phase=ControlDeliveryPhase.RESERVED,
                    backend_generation=snapshot.backend_generation,
                    native_session_id=snapshot.native_session_id,
                    payload=payload,
                )
                self._notify_reserved(on_reserved, operation_id, None)
            self._store.mark_control_operation_dispatched(
                operation_id,
                native_session_id=snapshot.native_session_id,
                updated_at=self._clock(),
            )
            return await self._deliver_settings(
                participant_id,
                operation_id,
                prepared.runtime,
                model,
                reasoning_effort,
            )

    async def _deliver_settings(
        self,
        participant_id: str,
        operation_id: str,
        runtime,
        model: str | None,
        reasoning_effort: str | None,
    ) -> SettingsOutcome:
        try:
            receipt = await runtime.update_settings(
                operation_id=operation_id,
                model=model,
                reasoning_effort=reasoning_effort,
            )
        except asyncio.CancelledError:
            self._settle_uncertain(
                operation_id,
                error=(
                    "the settings update was cancelled after transmission began; "
                    "its acknowledgement is unknown and it is never retried"
                ),
            )
            self._count_unknown_delivery(ControlKind.SETTINGS_UPDATE, CONTROL_UNKNOWN_ACK_LOST)
            raise
        except Exception as exc:
            self._store.settle_control_operation(
                operation_id,
                result=DeliveryResult.UNKNOWN,
                error_code=DELIVERY_UNKNOWN_ERROR_CODE,
                error=str(exc),
                updated_at=self._clock(),
            )
            logger.warning("settings update for %s is uncertain: %s", participant_id, exc)
            self._count_unknown_delivery(ControlKind.SETTINGS_UPDATE, CONTROL_UNKNOWN_ACK_LOST)
            return SettingsOutcome(applied=None, model=model, reasoning_effort=reasoning_effort)
        if not self._receipt_names_operation(operation_id, receipt):
            self._settle_uncertain(
                operation_id,
                error=(
                    f"the settings receipt named operation {receipt.operation_id!r}, "
                    f"not {operation_id!r}; the update is uncertain and the "
                    "receipt is not trusted to settle it"
                ),
            )
            self._count_unknown_delivery(
                ControlKind.SETTINGS_UPDATE, CONTROL_UNKNOWN_RECEIPT_MISMATCH
            )
            return SettingsOutcome(
                applied=None,
                model=model,
                reasoning_effort=reasoning_effort,
                error_code=DELIVERY_UNKNOWN_ERROR_CODE,
                error="the settings update stayed uncertain: the native receipt "
                "named a different operation",
            )
        self._settle_from_receipt(operation_id, receipt)
        if receipt.result is DeliveryResult.REJECTED:
            return SettingsOutcome(
                applied=False,
                model=model,
                reasoning_effort=reasoning_effort,
                error_code=receipt.error_code,
                error=receipt.error,
            )
        if receipt.result is DeliveryResult.UNKNOWN:
            logger.warning("settings update for %s stayed uncertain", participant_id)
            self._count_unknown_delivery(
                ControlKind.SETTINGS_UPDATE, CONTROL_UNKNOWN_RECEIPT_UNKNOWN
            )
            return SettingsOutcome(applied=None, model=model, reasoning_effort=reasoning_effort)
        return await self._read_back_settings(participant_id, runtime, model, reasoning_effort)

    async def _read_back_settings(
        self,
        participant_id: str,
        runtime,
        model: str | None,
        reasoning_effort: str | None,
    ) -> SettingsOutcome:
        try:
            fresh = await runtime.snapshot()
            self._gates.record_native_snapshot(participant_id, runtime, fresh)
        except Exception as exc:
            logger.warning(
                "settings update for %s was accepted but the effective-value "
                "readback failed: %s; the application stays uncertain",
                participant_id,
                exc,
            )
            self._count_unknown_delivery(
                ControlKind.SETTINGS_UPDATE, CONTROL_UNKNOWN_READBACK_FAILED
            )
            return SettingsOutcome(
                applied=None,
                model=model,
                reasoning_effort=reasoning_effort,
                error_code=DELIVERY_UNKNOWN_ERROR_CODE,
                error=(
                    "the native backend accepted the settings update, but the "
                    "effective-value readback failed; whether the application "
                    "took effect is unknown"
                ),
            )
        return SettingsOutcome(
            applied=True,
            model=fresh.settings.model,
            reasoning_effort=fresh.settings.reasoning_effort,
        )

    @staticmethod
    def _require_supported_settings(
        participant_id: str,
        snapshot: RuntimeSnapshot,
        model: str | None,
        reasoning_effort: str | None,
    ) -> None:
        requested_fields = {
            field_name
            for field_name, value in (
                (RuntimeSettingField.MODEL, model),
                (RuntimeSettingField.REASONING_EFFORT, reasoning_effort),
            )
            if value is not None
        }
        unsupported_fields = requested_fields - snapshot.settings.supported_fields
        if unsupported_fields:
            unsupported = ", ".join(sorted(str(field) for field in unsupported_fields))
            raise BadRequest(
                f"participant {participant_id!r} does not support updating settings "
                f"field(s): {unsupported}; no native mutation was attempted"
            )
