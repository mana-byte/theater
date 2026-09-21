"""MCP timing preserves payloads, cancellation, and content-free correlation."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.context import ServerRequestContext
from mcp.types import CallToolResult

from theater.daemon.server import Daemon
from theater.mcp.instrumentation import ToolTiming
from theater.observability import engine
from theater.observability.correlation import call_scope, current_call_id, extract_call_id


async def test_correlation_is_task_local_and_rejects_unbounded_metadata():
    async def identify():
        with call_scope() as call_id:
            await asyncio.sleep(0)
            assert current_call_id() == call_id
            return call_id

    first, second = await asyncio.gather(identify(), identify())
    assert first != second and current_call_id() is None
    for value in (None, 12, "f" * 33, "a" * 32 + "\n", "invalid"):
        assert extract_call_id({"theater_call_id": value}) is None


@pytest.mark.parametrize("outcome", ["success", "tool_error", "exception", "cancelled"])
async def test_middleware_preserves_outcomes_and_bounds_labels(outcome, monkeypatch, caplog):
    metrics = []

    class Bridge:
        def record(self, name, value, attrs):
            metrics.append((name, attrs))

    monkeypatch.setattr(engine, "_bridge", Bridge())
    caplog.set_level(logging.DEBUG, logger="theater.timing")
    context = ServerRequestContext(
        session=None,
        lifespan_context={},
        protocol_version="2025-11-25",
        method="tools/call",
        params={"name": "private-name\n" * 100, "arguments": {"prompt": "secret-prompt"}},
    )
    result = CallToolResult(content=[], is_error=outcome == "tool_error")
    error = asyncio.CancelledError() if outcome == "cancelled" else ValueError("secret-error")

    async def next_call(ctx):
        assert ctx is context
        assert extract_call_id({"theater_call_id": current_call_id()}) is not None
        if outcome in {"exception", "cancelled"}:
            raise error
        return result

    middleware = ToolTiming(frozenset({"whoami"}), "control")
    if outcome in {"exception", "cancelled"}:
        with pytest.raises(type(error)) as caught:
            await middleware(context, next_call)
        assert caught.value is error
    else:
        assert await middleware(context, next_call) is result
    assert current_call_id() is None
    expected = {"tool_error": "error", "exception": "error"}.get(outcome, outcome)
    assert metrics == [
        ("theater.mcp.tool.duration", {"tool": "unknown", "lane": "control", "result": expected})
    ]
    assert not any(
        value in caplog.text for value in ("private-name", "secret-prompt", "secret-error")
    )


async def test_stdio_timing_correlates_daemon_without_payload_changes(
    theater_home, tmp_path, caplog
):
    caplog.set_level(logging.DEBUG, logger="theater.timing")
    daemon = Daemon()
    await daemon.start()
    log = tmp_path / "mcp-timing.log"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "theater.cli",
            "mcp",
            "--id",
            "p-timing",
            "--harness",
            "vibe",
            "--timing-log",
            str(log),
        ],
        env={"THEATER_HOME": str(theater_home)},
    )
    try:
        async with (
            stdio_client(parameters) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool("whoami", {})
            assert not result.is_error, result.content
            payload = result.structured_content or json.loads(result.content[0].text)
            assert payload["id"] == "p-timing"
            refused = await session.call_tool("load_skill", {"name": "secret-missing-skill"})
            assert refused.is_error
        text = log.read_text()
        assert "mcp.tool whoami" in text and "mcp.tool load_skill" in text
        assert "result=error" in text and "secret-missing-skill" not in text
        assert all(f"{phase}=" in text for phase in ("lock_wait_ms", "connect_ms", "roundtrip_ms"))
        tool_calls = re.findall(r"mcp\.tool .*? call=([0-9a-f]{32})", text)
        assert len(tool_calls) == len(set(tool_calls)) == 2
        daemon_calls = {r.__dict__.get("theater.call_id") for r in caplog.records}
        assert set(tool_calls) <= daemon_calls
    finally:
        await daemon.aclose()
