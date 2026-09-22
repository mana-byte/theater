"""Verify local telemetry with read-only MCP calls; never register an agent."""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from theater import config, paths

GRAFANA = "http://127.0.0.1:3000/api/datasources/proxy/uid"


def query(source: str, path: str, **params: str) -> dict:
    url = f"{GRAFANA}/{source}/{path}?{urlencode(params)}"
    with urlopen(url, timeout=5) as response:
        return json.load(response)


async def exercise() -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "theater.cli", "mcp", "--toolset", "control", "--timing"],
        env={"THEATER_HOME": str(paths.home())},
    )
    durations = []
    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        for _ in range(4):
            started = time.perf_counter()
            result = await session.call_tool("list_models", {})
            if result.is_error:
                raise RuntimeError("The read-only list_models probe failed")
            durations.append((time.perf_counter() - started) * 1000)
            await asyncio.sleep(2)
    print(f"Warm MCP round-trip median: {statistics.median(durations[1:]):.2f} ms")


def verify(started_ns: int, service_name: str) -> str | None:
    selector = f"service_name={json.dumps(service_name)}"
    logs = query(
        "loki",
        "loki/api/v1/query_range",
        query=f'{{{selector}}} | theater_process_role="mcp" |= "mcp.tool list_models"',
        start=str(started_ns),
        limit="1",
    )["data"]["result"]
    if not logs:
        return None
    stream = logs[0]["stream"]
    trace_id = stream["trace_id"]
    instance = stream["service_instance_id"]
    trace = query("tempo", f"api/traces/{trace_id}")
    spans = {
        span["name"]: span
        for batch in trace.get("batches", [])
        for scope in batch.get("scopeSpans", [])
        for span in scope.get("spans", [])
    }
    root = spans.get("tools/call list_models")
    client = spans.get("rpc.client models")
    pool = spans.get("rpc.pool_wait models")
    daemon = spans.get("rpc.server models")
    if not all((root, pool, client, daemon)):
        return None
    assert root is not None and pool is not None and client is not None and daemon is not None
    assert daemon["parentSpanId"] == client["spanId"]
    assert client["parentSpanId"] == root["spanId"]
    assert pool["parentSpanId"] == root["spanId"]
    attributes = {entry["key"]: entry["value"] for entry in client["attributes"]}
    assert all(
        f"theater.{name}" in attributes
        for name in (
            "call_id",
            "lock_wait_ms",
            "connect_ms",
            "roundtrip_ms",
            "duration_ms",
            "wall_duration_ms",
            "clock_gap_ms",
            "clock_discontinuity",
        )
    )
    metric = query(
        "prometheus",
        "api/v1/query",
        query=(
            "histogram_count(theater_mcp_tool_duration_milliseconds{"
            f'{selector},service_instance_id={json.dumps(instance)},tool="list_models"'
            "})"
        ),
    )["data"]["result"]
    if not metric or float(metric[0]["value"][1]) < 4:
        return None
    return trace_id


def main() -> None:
    settings = config.load().observability
    if not settings.otlp_enabled:
        raise SystemExit("Enable local OTLP in Theater's config and restart the daemon first.")
    port = 4318 if settings.otlp_protocol == "http" else 4317
    local_endpoints = {None, f"http://localhost:{port}", f"http://127.0.0.1:{port}"}
    if settings.otlp_endpoint not in local_endpoints:
        raise SystemExit("This check requires Theater's OTLP endpoint to point to the local stack.")
    started_ns = time.time_ns()
    asyncio.run(exercise())
    deadline = time.monotonic() + 45
    last_error = "signals have not arrived yet"
    while time.monotonic() < deadline:
        try:
            trace_id = verify(started_ns, settings.service_name)
            if trace_id is not None:
                print(f"Verified MCP metrics, logs, and MCP → client → daemon trace: {trace_id}")
                return
        except OSError as exc:
            last_error = str(exc)
        time.sleep(2)
    raise SystemExit(f"Local telemetry verification timed out: {last_error}")


if __name__ == "__main__":
    main()
