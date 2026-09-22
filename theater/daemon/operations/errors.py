"""Stable domain failures for durable public operations."""

from __future__ import annotations

from theater.models import TheaterError


class IdempotencyConflict(TheaterError):
    code = "idempotency_conflict"

    def __init__(
        self,
        *,
        client_id: str,
        key: str,
        requested_method: str,
        original_method: str,
        operation_id: str | None,
    ) -> None:
        self.details = {
            "client_id": client_id,
            "idempotency_key": key,
            "requested_method": requested_method,
            "original_method": original_method,
            "operation_id": operation_id,
        }
        super().__init__(
            "the idempotency key was already accepted with a different method or payload; "
            "inspect the original result and retry new work with a new key"
        )


class OperationNotFound(TheaterError):
    code = "not_found"

    def __init__(self, operation_id: str) -> None:
        self.details = {"operation_id": operation_id}
        super().__init__(f"no public operation {operation_id!r} exists")


class InvalidOperationTransition(TheaterError):
    code = "bad_request"

    def __init__(self, operation_id: str, current: str, requested: str) -> None:
        self.details = {
            "operation_id": operation_id,
            "current_state": current,
            "requested_state": requested,
        }
        super().__init__(
            f"operation {operation_id!r} cannot transition from {current!r} to {requested!r}"
        )


__all__ = ["IdempotencyConflict", "InvalidOperationTransition", "OperationNotFound"]
