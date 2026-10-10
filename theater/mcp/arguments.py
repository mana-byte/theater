"""Report arguments a tool does not take, instead of dropping them silently."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping

import pydantic_core
from mcp.types import CallToolResult, TextContent

#: Stale names agents keep passing, with the replacement they were looking for.
STALE_ARGUMENT_HINTS: Mapping[str, str] = {
    "tail": "removed; page with next_cursor",
    "key": "use keys (a list)",
    "limit": "not supported here; page with next_cursor where offered",
    "harness": "not a filter; the result covers every harness",
    "background": "not supported; spawn returns at once, then await_sessions",
}


def ignored_arguments(arguments: Mapping[str, object], accepted: Iterable[str]) -> list:
    """Names (or {name: hint}) of the supplied arguments outside the tool's parameters."""
    known = set(accepted)
    return [
        {name: STALE_ARGUMENT_HINTS[name]} if name in STALE_ARGUMENT_HINTS else name
        for name in arguments
        if name not in known
    ]


def with_ignored_arguments(result: CallToolResult, ignored: list) -> CallToolResult:
    """Add ignored_arguments to a successful dict result; other shapes stay untouched."""
    if not ignored or result.is_error:
        return result
    structured = result.structured_content
    # Untyped `-> dict` tools carry their payload only as JSON text.
    textual = structured is None
    if textual and len(result.content) == 1 and isinstance(result.content[0], TextContent):
        try:
            structured = json.loads(result.content[0].text)
        except ValueError:
            return result
    if not isinstance(structured, dict) or set(structured) == {"result"}:
        return result
    payload = {**structured, "ignored_arguments": ignored}
    text = pydantic_core.to_json(payload, fallback=str, indent=2).decode()
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=None if textual else payload,
    )


__all__ = ["STALE_ARGUMENT_HINTS", "ignored_arguments", "with_ignored_arguments"]
