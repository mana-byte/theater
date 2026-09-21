"""MCP timing through the SDK's supported middleware, without duplicate spans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.types import CallToolResult

from theater.observability.catalog import MCP_TOOL
from theater.observability.correlation import call_scope
from theater.observability.engine import span


@dataclass(frozen=True, slots=True)
class ToolTiming:
    tools: frozenset[str]
    lane: str

    async def __call__(
        self, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        if ctx.method != "tools/call":
            return await call_next(ctx)
        name = ctx.params.get("name") if ctx.params else None
        tool = name if isinstance(name, str) and name in self.tools else "unknown"
        with (
            call_scope() as call_id,
            span(
                MCP_TOOL,
                tool=tool,
                lane=self.lane,
                call_id=call_id,
                slow_ms=float("inf") if tool == "await_sessions" else None,
            ) as fields,
        ):
            result = await call_next(ctx)
            match result:
                case CallToolResult(is_error=True) | {"isError": True}:
                    fields.set_result("error", error_type="tool_error")
            return result
