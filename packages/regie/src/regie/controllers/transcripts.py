"""Explicit, retry-safe transcript binding for the Régie recovery flow."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from theater.frontend import (
    FrontendClient,
    FrontendClientError,
    FrontendResponseError,
    FrontendTransportError,
    TranscriptBindResult,
)


class TranscriptBindState(StrEnum):
    PENDING = "pending"
    UNCERTAIN = "uncertain"
    REFUSED = "refused"
    SUCCEEDED = "succeeded"


@dataclass(slots=True)
class TranscriptBindRecord:
    participant_id: str
    location: str
    prior_owner_id: str | None
    idempotency_key: str
    state: TranscriptBindState = TranscriptBindState.PENDING
    result: TranscriptBindResult | None = None
    detail: str | None = None


class TranscriptBindingController:
    """Keep the original parameters and key when a bind outcome is uncertain."""

    def __init__(self, client: FrontendClient) -> None:
        self._client = client
        self._records: dict[tuple[str, str], TranscriptBindRecord] = {}

    def record(self, participant_id: str, location: str) -> TranscriptBindRecord | None:
        return self._records.get((participant_id, location))

    async def bind(
        self,
        participant_id: str,
        location: str,
        *,
        prior_owner_id: str | None,
    ) -> TranscriptBindRecord:
        identity = (participant_id, location)
        existing = self._records.get(identity)
        if existing is not None and existing.state is TranscriptBindState.PENDING:
            return existing
        if existing is not None and existing.state is TranscriptBindState.UNCERTAIN:
            record = existing
        else:
            record = TranscriptBindRecord(
                participant_id,
                location,
                prior_owner_id,
                uuid4().hex,
            )
            self._records[identity] = record
        return await self._invoke(record)

    async def _invoke(self, record: TranscriptBindRecord) -> TranscriptBindRecord:
        record.state = TranscriptBindState.PENDING
        record.detail = None
        try:
            response = await self._client.transcripts.bind(
                record.participant_id,
                record.location,
                idempotency_key=record.idempotency_key,
                **(
                    {"prior_owner_id": record.prior_owner_id}
                    if record.prior_owner_id is not None
                    else {}
                ),
            )
        except asyncio.CancelledError:
            record.state = TranscriptBindState.UNCERTAIN
            record.detail = "local wait was cancelled after the bind started"
            raise
        except FrontendResponseError as exc:
            record.state = TranscriptBindState.REFUSED
            record.detail = f"{exc.value.code}: {exc.value.message}"
        except FrontendTransportError as exc:
            record.state = TranscriptBindState.UNCERTAIN
            record.detail = str(exc)
        except FrontendClientError as exc:
            record.state = TranscriptBindState.REFUSED
            record.detail = str(exc)
        else:
            record.state = TranscriptBindState.SUCCEEDED
            record.result = response.value
        return record


__all__ = [
    "TranscriptBindRecord",
    "TranscriptBindState",
    "TranscriptBindingController",
]
