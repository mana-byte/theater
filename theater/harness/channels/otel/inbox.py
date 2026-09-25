"""Bounded in-memory native OTel delivery queues."""

from __future__ import annotations

from dataclasses import dataclass

from theater.constants.harness import HARNESS_OTEL_DEDUPE_MAX_DELIVERIES
from theater.harness.channels.inbox import DeliveryInbox, EnqueueResult
from theater.harness.contracts.channels import (
    OtelRecord,
    OtelSignal,
)


@dataclass(frozen=True, slots=True)
class OtelDelivery:
    """One accepted bounded native OTel record."""

    signal: OtelSignal
    binding_name: str
    record: OtelRecord
    native_id: str
    delivery_id: str


OtelEnqueueResult = EnqueueResult


class OtelInbox(DeliveryInbox[OtelDelivery]):
    """One nonblocking queue per participant and native OTel channel."""

    DEDUPE_MAX_DELIVERIES = HARNESS_OTEL_DEDUPE_MAX_DELIVERIES
    OVERFLOW_REASON = "native OTel inbox overflow"


__all__ = ["OtelDelivery", "OtelEnqueueResult", "OtelInbox"]
