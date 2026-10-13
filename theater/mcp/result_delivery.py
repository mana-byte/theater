"""Add native MCP values to JSON-object results without changing their text rendering."""

from __future__ import annotations

from mcp.types import CallToolResult, TextContent

from theater.json_values import decode_finite_json

NATIVE_OBJECT_TOOLS = frozenset(
    {
        "whoami",
        "spawn_session",
        "send",
        "cancel_job",
        "get_session_controls",
        "interrupt_session",
        "update_participant",
        "update_session_settings",
        "scratchpad_write",
        "scratchpad_delete",
        "terminate_session",
        "adopt_session",
    }
)


def with_structured_object(result: CallToolResult, *, tool_name: str) -> CallToolResult:
    """Expose small control results as native values, leaving bulk text responses alone."""
    if (
        tool_name not in NATIVE_OBJECT_TOOLS
        or result.is_error
        or result.structured_content is not None
        or len(result.content) != 1
    ):
        return result
    content = result.content[0]
    if not isinstance(content, TextContent):
        return result
    valid, value = decode_finite_json(content.text)
    if not valid or not isinstance(value, dict):
        return result
    return result.model_copy(update={"structured_content": value})


__all__ = ["with_structured_object"]
