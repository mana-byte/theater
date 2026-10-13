"""One refusal envelope for every harness, delivered the way its runtime can read it.

Vibe drops the content of an isError result, so its manifest asks for "result" delivery:
the same envelope arrives as ordinary text that the model can see.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import CallToolResult, TextContent

from theater.constants.refusals import NEXT_DAEMON_UNAVAILABLE
from theater.harness.registry.mcp import ErrorDelivery
from theater.models import TheaterError
from theater.protocol import RemoteError

DAEMON_UNAVAILABLE = "daemon_unavailable"
_ENVELOPE_KEYS = frozenset({"next", "side_effect", "retryable"})
# DaemonClient raises ConnectionError/TimeoutError; other OSErrors are not the daemon's fault.
_CAUSES = (RemoteError, TheaterError, ConnectionError, TimeoutError, EOFError)
_RETRYABLE: dict[str, bool | str] = {
    "busy": "when idle",
    "human_present": "after the human leaves",
    "awaiting_decision": "after a human answers the dialog",
    DAEMON_UNAVAILABLE: "after checking",
}


def _cause(exc: BaseException) -> BaseException | None:
    """Find the Theater refusal or transport failure an SDK ToolError wraps."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, _CAUSES):
            return current
        current = current.__cause__
    return None


def refusal_envelope(exc: BaseException) -> dict[str, Any] | None:
    """Map a refusal to {"ok": false, "error": {...}}; None leaves the SDK's own handling."""
    cause = _cause(exc)
    if isinstance(cause, RemoteError):
        code, message, details = cause.code, cause.message, dict(cause.details or {})
    elif isinstance(cause, TheaterError):
        code, message, details = cause.code, str(cause), dict(cause.details or {})
    elif isinstance(cause, (ConnectionError, TimeoutError, EOFError)):
        code = DAEMON_UNAVAILABLE
        message = f"the Theater daemon did not answer: {cause or type(cause).__name__}"
        details = {"side_effect": "unknown", "next": NEXT_DAEMON_UNAVAILABLE}
    else:
        return None
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": details.get("retryable", _RETRYABLE.get(code, False)),
    }
    for key in ("next", "side_effect"):
        if details.get(key) is not None:
            error[key] = details[key]
    extra = {k: v for k, v in details.items() if k not in _ENVELOPE_KEYS and v is not None}
    if extra:
        error["details"] = extra
    return {"ok": False, "error": error}


def refusal_result(envelope: dict[str, Any], delivery: ErrorDelivery) -> CallToolResult:
    """Render the envelope as an isError result or, for "result" delivery, as plain text."""
    error = envelope["error"]
    head = f"{error['code']}: {error['message']}"
    if delivery == "result":
        head = f"REFUSED {head}"
    line = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), default=str)
    return CallToolResult(
        content=[TextContent(type="text", text=f"{head}\n{line}")],
        structured_content=envelope,
        is_error=delivery == "is_error",
    )


def refusal_code(result: object) -> str | None:
    """The envelope's code in a tool result of either delivery form, else None."""
    if isinstance(result, CallToolResult):
        structured = result.structured_content
    elif isinstance(result, dict):
        structured = result.get("structuredContent")
    else:
        return None
    if not isinstance(structured, dict) or structured.get("ok") is not False:
        return None
    error = structured.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


__all__ = ["DAEMON_UNAVAILABLE", "refusal_code", "refusal_envelope", "refusal_result"]
