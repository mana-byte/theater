"""Persist unread native outcomes before replacing a disconnected live registration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from theater.daemon.observation.live import LiveRegistration
from theater.harness.contracts.source import Batch


async def persist_buffered_evidence(
    registration: LiveRegistration | None,
    *,
    backend_generation: int,
    native_session_id: str,
    validate_owner: Callable[[], Awaitable[None]],
) -> None:
    """Leave the source snapshot intact so a failed handoff remains retryable."""
    if registration is None:
        return
    if (
        registration.backend_generation != backend_generation
        or registration.native_session_id != native_session_id
    ):
        raise RuntimeError("buffered terminal evidence belongs to another live registration")
    batch = Batch(terminal_evidence=registration.live_source.buffered_terminal_evidence())
    for outcome in batch.terminal_evidence:
        await validate_owner()
        if registration.evidence_sink is None:
            raise RuntimeError("buffered terminal evidence has no registered durable sink")
        await registration.evidence_sink(
            registration.participant_id,
            backend_generation=registration.backend_generation,
            outcome=outcome,
        )
        await validate_owner()
