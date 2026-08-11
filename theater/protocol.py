"""Wire format between the daemon and everything that talks to it.

Newline-delimited JSON over a unix socket. Not JSON-RPC: we do not need
batching, notifications or the error-code registry, and hand-rolling twenty
lines beats depending on a framing library for a local socket.

    -> {"id": 1, "method": "participants.list", "params": {}}
    <- {"id": 1, "ok": true, "result": [...]}
    <- {"id": 1, "ok": false, "error": {"code": "not_found", "message": "..."}}

Every response carries the id of its request, so a client may pipeline.
"""

from __future__ import annotations

import json
from typing import Any

#: Bumped when the shape of a request or response changes incompatibly. The
#: daemon refuses clients from a different major.
PROTOCOL_VERSION = 1


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
