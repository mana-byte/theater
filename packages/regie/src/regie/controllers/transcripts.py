"""Explicit, retry-safe transcript binding for the Régie recovery flow."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from theater.frontend import (
    FrontendClient,
    FrontendClientError,
    FrontendResponseError,
    FrontendTransportError,
    ResponseCorrelationError,
    ResponseValidationError,
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


type ClientFactory = Callable[[], FrontendClient]


class TranscriptBindingController:
    """Keep the original parameters and key when a bind outcome is uncertain."""

    def __init__(
        self,
        client: FrontendClient,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._client = client
        self._client_factory = client_factory
        self._records: dict[tuple[str, str], TranscriptBindRecord] = {}
        self._clients: dict[tuple[str, str], FrontendClient] = {}
        self._owned_clients: dict[int, FrontendClient] = {}
        self._closed = False

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
            bind_client = (
                self._client_factory() if self._client_factory is not None else self._client
            )
            self._clients[identity] = bind_client
            if self._client_factory is not None and bind_client is not self._client:
                self._owned_clients[id(bind_client)] = bind_client
        return await self._invoke(identity, record)

    async def _invoke(
        self,
        identity: tuple[str, str],
        record: TranscriptBindRecord,
    ) -> TranscriptBindRecord:
        if self._closed:
            record.state = TranscriptBindState.UNCERTAIN
            record.detail = "Régie is closing; the transcript bind was not retried"
            await self._release_client(identity)
            return record
        record.state = TranscriptBindState.PENDING
        record.detail = None
        client = self._clients.get(identity, self._client)
        try:
            response = await client.transcripts.bind(
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
            await self._release_client(identity)
        except FrontendTransportError as exc:
            record.state = TranscriptBindState.UNCERTAIN
            record.detail = str(exc)
        except (ResponseCorrelationError, ResponseValidationError) as exc:
            record.state = TranscriptBindState.UNCERTAIN
            record.detail = f"cannot verify transcript bind response: {exc}"
        except FrontendClientError as exc:
            record.state = TranscriptBindState.REFUSED
            record.detail = str(exc)
            await self._release_client(identity)
        except TypeError as exc:
            record.state = TranscriptBindState.UNCERTAIN
            record.detail = f"cannot decode transcript bind response: {exc}"
        else:
            record.state = TranscriptBindState.SUCCEEDED
            record.result = response.value
            await self._release_client(identity)
        return record

    async def _release_client(self, identity: tuple[str, str]) -> None:
        client = self._clients.pop(identity, None)
        if client is None or any(retained is client for retained in self._clients.values()):
            return
        owned = self._owned_clients.pop(id(client), None)
        if owned is not None:
            with contextlib.suppress(Exception):
                await owned.close()

    async def close(self) -> None:
        self._closed = True
        for client in tuple(self._owned_clients.values()):
            with contextlib.suppress(Exception):
                await client.close()
        self._clients.clear()
        self._owned_clients.clear()


__all__ = [
    "TranscriptBindRecord",
    "TranscriptBindState",
    "TranscriptBindingController",
]
