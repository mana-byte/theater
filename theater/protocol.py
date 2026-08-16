"""Wire format between the daemon and everything that talks to it.

Newline-delimited JSON over a unix socket. Not JSON-RPC: we do not need
batching, notifications or the error-code registry, and hand-rolling twenty
lines beats depending on a framing library for a local socket.

    -> {"id": 1, "method": "participants.list", "params": {}}
    <- {"id": 1, "ok": true, "result": [...]}
    <- {"id": 1, "ok": false, "error": {"code": "not_found", "message": "..."}}

Every response carries the id of its request, so a client may pipeline.

One message is one line, which makes the maximum line length part of the wire
format rather than a detail of either end -- hence MAX_MESSAGE_BYTES and the
two readers below living here, where both ends read them from the same place.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

#: Bumped when the shape of a request or response changes incompatibly. The
#: daemon refuses clients from a different major.
PROTOCOL_VERSION = 1

#: Longest single message either end will read, in bytes.
#:
#: asyncio's 64 KiB default is far too small — whole transcripts, deep bus
#: tails and a prompt carrying a pasted file all cross it routinely, and the
#: overrun is not a clean error (see `read_message`). 64 MiB is a thousandfold
#: headroom and still a limit: it caps what a peer that never sends a newline
#: can make the other end buffer.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024


class MessageTooLarge(ConnectionError):
    """A peer sent a line longer than MAX_MESSAGE_BYTES.

    Deliberately a ConnectionError. After an overrun the stream is *not*
    positioned at a message boundary -- asyncio drops the bytes it had
    buffered and leaves the rest of the oversized line on the wire -- so the
    only safe reactions are to drop the connection or to drain it explicitly.
    Both ends already treat ConnectionError as "this connection is finished",
    so inheriting from it routes the failure into machinery that exists.
    """


async def read_message(reader: asyncio.StreamReader) -> bytes:
    """Read one message, reporting an overrun as a connection fault.

    Returns b"" at end of stream, as ``readline`` does.

    Not ``readline``, for two reasons. It signals an overrun by raising a
    bare ``ValueError("Separator is not found, and chunk exceed the limit")``,
    which is indistinguishable from a programming error and was caught by
    nobody. And it *discards the entire buffer* on the way out -- including
    any complete message that had already arrived behind the oversized one,
    which the protocol allows a client to pipeline. ``readuntil`` leaves the
    buffer exactly where it was, so drain_message below can consume precisely
    the oversized line and nothing more.
    """
    try:
        return await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError as exc:
        return exc.partial
    except asyncio.LimitOverrunError as exc:
        raise MessageTooLarge(f"message exceeds {MAX_MESSAGE_BYTES} bytes: {exc}") from exc


async def drain_message(reader: asyncio.StreamReader) -> None:
    """Discard the rest of an oversized line, restoring stream sync.

    For a reader that would rather keep the connection than hang up -- the
    daemon, which should not make one enormous prompt cost an agent the rest
    of its session.

    ``LimitOverrunError.consumed`` is the offset the separator search reached,
    so those bytes are known not to contain a newline and can be dropped
    without looking at them. Repeat until the newline turns up; memory stays
    bounded by the limit however long the line is. EOF ends it too -- there is
    then nothing left to resynchronise with.
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


def request(req_id: int, method: str, params: dict[str, Any] | None = None) -> bytes:
    return encode({"id": req_id, "method": method, "params": params or {}})


def ok(req_id: int, result: Any) -> bytes:
    return encode({"id": req_id, "ok": True, "result": result})


def err(req_id: int, code: str, message: str) -> bytes:
    return encode({"id": req_id, "ok": False, "error": {"code": code, "message": message}})


class RemoteError(Exception):
    """An error the daemon reported, re-raised on the client side."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
