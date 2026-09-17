"""Thin public handlers for provider-backed participant launch and adoption."""

from __future__ import annotations

from types import MappingProxyType

from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.spawning.service import ParticipantLaunchService


async def participants_spawn(
    daemon,
    context: ConnectionContext,
    params: dict,
    *,
    idempotency_key: str,
) -> object:
    return ParticipantLaunchService(daemon).spawn(
        client_id=context.client_id,
        idempotency_key=idempotency_key,
        params=params,
    )


async def participants_adopt(
    daemon,
    context: ConnectionContext,
    params: dict,
    *,
    idempotency_key: str,
) -> object:
    return ParticipantLaunchService(daemon).adopt(
        client_id=context.client_id,
        idempotency_key=idempotency_key,
        params=params,
    )


PARTICIPANT_HANDLERS = MappingProxyType(
    {
        "frontend.participants.spawn": participants_spawn,
        "frontend.participants.adopt": participants_adopt,
    }
)

__all__ = ["PARTICIPANT_HANDLERS"]
