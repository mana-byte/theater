"""Wire format: newline-delimited JSON over a unix socket, every reply carrying its request id.
One message is one line, so MAX_MESSAGE_BYTES is part of the wire format and lives here for both
ends.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

#: Bumped when the request/response shape changes incompatibly; daemon refuses different majors.
PROTOCOL_VERSION = 1
# Extension: optional top-level _meta carries W3C trace context; receivers ignore unknown/malformed.

#: Longest message either end reads. 64 MiB — headroom for transcripts, caps non-terminating peers.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024


class MessageTooLarge(ConnectionError):
    """A peer sent a line longer than MAX_MESSAGE_BYTES.
    A ConnectionError because the stream is no longer at a message boundary: drop or explicitly
    drain.
    """


async def read_message(reader: asyncio.StreamReader) -> bytes:
    """Read one message (b"" at EOF), reporting an overrun as a connection fault.
    Not ``readline``: its overrun is a bare ValueError and it discards pipelined messages behind the
    line.
    """
    try:
        return await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError as exc:
        return exc.partial
    except asyncio.LimitOverrunError as exc:
        raise MessageTooLarge(f"message exceeds {MAX_MESSAGE_BYTES} bytes: {exc}") from exc


async def drain_message(reader: asyncio.StreamReader) -> None:
    """Discard the rest of an oversized line, restoring stream sync with bounded memory.

    Lets the daemon keep a connection rather than cost an agent its session over one huge prompt.
    """
    while True:
        try:
            await reader.readuntil(b"\n")
        except asyncio.IncompleteReadError:
            return
        except asyncio.LimitOverrunError as exc:
            try:
                await reader.readexactly(max(exc.consumed, 1))
            except asyncio.IncompleteReadError:
                return
        else:
            return


def encode(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


def request(
    req_id: int,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    meta: Mapping[str, Any] | None = None,
) -> bytes:
    payload = {"id": req_id, "method": method, "params": params or {}}
    if meta:
        payload["_meta"] = dict(meta)
    return encode(payload)


def ok(req_id: int, result: Any) -> bytes:
    return encode({"id": req_id, "ok": True, "result": result})


def err(
    req_id: int,
    code: str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> bytes:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = dict(details)
    return encode({"id": req_id, "ok": False, "error": error})


class RemoteError(Exception):
    """An error the daemon reported, re-raised on the client side."""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = dict(details) if details is not None else None
