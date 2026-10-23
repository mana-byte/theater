"""Shared acknowledgement and receipt settlement for non-send native controls."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from theater.daemon.controls._common import (
    CONTROL_UNKNOWN_ACK_LOST,
    CONTROL_UNKNOWN_RECEIPT_MISMATCH,
    DELIVERY_UNKNOWN_ERROR_CODE,
)
from theater.daemon.controls._host import ControlHost
from theater.harness.contracts.runtime import ControlKind, ControlReceipt, DeliveryResult


@dataclass(frozen=True, slots=True)
class NativeAcknowledgementLost:
    error: Exception


@dataclass(frozen=True, slots=True)
class NativeReceiptMismatch:
    receipt: ControlReceipt


@dataclass(frozen=True, slots=True)
class NativeReceiptSettled:
    receipt: ControlReceipt


type NativeTransmission = NativeAcknowledgementLost | NativeReceiptMismatch | NativeReceiptSettled


async def transmit_native_action(
    host: ControlHost,
    *,
    operation_id: str,
    kind: ControlKind,
    transmit: Callable[[], Awaitable[ControlReceipt]],
    cancelled_error: str,
    mismatch_error: Callable[[ControlReceipt], str],
) -> NativeTransmission:
    """Settle transmission once; action policy interprets the typed result."""
    try:
        receipt = await transmit()
    except asyncio.CancelledError:
        host._settle_uncertain(operation_id, error=cancelled_error)
        host._count_unknown_delivery(kind, CONTROL_UNKNOWN_ACK_LOST)
        raise
    except Exception as exc:
        host._store.settle_control_operation(
            operation_id,
            result=DeliveryResult.UNKNOWN,
            error_code=DELIVERY_UNKNOWN_ERROR_CODE,
            error=str(exc),
            updated_at=host._clock(),
        )
        return NativeAcknowledgementLost(exc)
    if not host._receipt_names_operation(operation_id, receipt):
        host._settle_uncertain(operation_id, error=mismatch_error(receipt))
        host._count_unknown_delivery(kind, CONTROL_UNKNOWN_RECEIPT_MISMATCH)
        return NativeReceiptMismatch(receipt)
    host._settle_from_receipt(operation_id, receipt)
    return NativeReceiptSettled(receipt)
