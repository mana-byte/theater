"""Bounded in-memory hook delivery queues."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from theater.constants.harness import HARNESS_HOOK_DEDUPE_MAX_DELIVERIES
from theater.harness.channels.inbox import DeliveryInbox, EnqueueResult
from theater.harness.contracts.callbacks import HookAdmissionIdentity
from theater.harness.contracts.values import freeze_json_mapping


@dataclass(frozen=True, slots=True)
class HookDelivery:
    """One accepted opaque hook envelope and optional daemon admission snapshot."""

    event: str
    payload: Mapping[str, object]
    native_id: str
    delivery_id: str | None = None
    admission_identity: HookAdmissionIdentity | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise TypeError("hook payload must be a mapping")
        object.__setattr__(
            self,
            "payload",
            freeze_json_mapping(self.payload),
        )
        if not isinstance(self.native_id, str) or not self.native_id.strip():
            raise TypeError("hook delivery native_id must be a non-blank string")
        if self.admission_identity is not None and not isinstance(
            self.admission_identity, HookAdmissionIdentity
        ):
            raise TypeError(
                "hook delivery admission_identity must be HookAdmissionIdentity or null"
            )


HookEnqueueResult = EnqueueResult


class HookInbox(DeliveryInbox[HookDelivery]):
    """One nonblocking queue per participant and hook channel."""

    DEDUPE_MAX_DELIVERIES = HARNESS_HOOK_DEDUPE_MAX_DELIVERIES
    OVERFLOW_REASON = "hook inbox overflow"


__all__ = ["HookDelivery", "HookEnqueueResult", "HookInbox"]
