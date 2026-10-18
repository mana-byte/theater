"""Public-socket and journal helpers for the daemon state-stream tests."""

from __future__ import annotations

import asyncio
import json

from tests.support.frontend_wire import handshake
from theater import paths, protocol
from theater.models import (
    JournalEventRecord,
)


async def public_connection(client_id: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_unix_connection(
        str(paths.socket_path()), limit=max(protocol.MAX_MESSAGE_BYTES, 1024)
    )
    writer.write(handshake(client_id))
    await writer.drain()
    response = json.loads(await protocol.read_message(reader))
    assert response["ok"] is True
    return reader, writer


async def call_public(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, frame: bytes
) -> dict:
    writer.write(frame)
    await writer.drain()
    return json.loads(await protocol.read_message(reader))


async def close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    await writer.wait_closed()


def journal_record(entity_id: str, revision: int = 1) -> JournalEventRecord:
    return JournalEventRecord(
        kind="participant.updated",
        entity_id=entity_id,
        entity_revision=revision,
        payload={"participant_id": entity_id},
        recorded_at=1.0,
    )
