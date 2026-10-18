"""Raw public-socket frames: build a request or handshake, and exchange frames with the daemon."""

from __future__ import annotations

import asyncio
import json

from theater import paths, protocol
from theater.frontend import (
    StateProjection,
)
from theater.frontend.capabilities import PUBLIC_API_MAJOR, PUBLIC_API_MINOR


def request(request_id: int, method: str, params: dict | None = None, **extra: object) -> bytes:
    """One encoded request frame; `extra` adds top-level fields the schema may reject."""
    return protocol.encode({"id": request_id, "method": method, "params": params or {}, **extra})


def handshake(
    client_id: str = "test-client",
    *,
    request_id: int = 1,
    major: int = PUBLIC_API_MAJOR,
    minor: int = PUBLIC_API_MINOR,
    required: list[str] | None = None,
    role: str = "operator",
    channel: str = "rpc",
    **provider: str,
) -> bytes:
    """A `frontend.handshake` frame; `provider` adds provider identity fields as given."""
    params = {
        "api": {"major": major, "minor": minor},
        "client_id": client_id,
        "role": role,
        "channel": channel,
        "required_capabilities": required or [],
        **provider,
    }
    return request(request_id, "frontend.handshake", params)


async def exchange(frames: list[bytes]) -> list[dict]:
    """Send each frame on one connection to the daemon and collect one reply per frame."""
    reader, writer = await asyncio.open_unix_connection(
        str(paths.socket_path()), limit=max(protocol.MAX_MESSAGE_BYTES, 1024)
    )
    try:
        responses: list[dict] = []
        for frame in frames:
            writer.write(frame)
            await writer.drain()
            responses.append(json.loads(await protocol.read_message(reader)))
        return responses
    finally:
        writer.close()
        await writer.wait_closed()


async def read_request(reader: asyncio.StreamReader) -> dict[str, object] | None:
    frame = await reader.readline()
    if not frame:
        return None
    assert frame.endswith(b"\n")
    value = json.loads(frame)
    assert isinstance(value, dict)
    return value


def projection_wire(projection: StateProjection) -> dict[str, dict[str, object]]:
    return {
        "participants": {key: value.to_wire() for key, value in projection.participants.items()},
        "operations": {key: value.to_wire() for key, value in projection.operations.items()},
        "jobs": {key: value.to_wire() for key, value in projection.jobs.items()},
        "providers": {key: value.to_wire() for key, value in projection.providers.items()},
        "workspaces": {key: value.to_wire() for key, value in projection.workspaces.items()},
    }
