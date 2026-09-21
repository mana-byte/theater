"""Long-lived health monitors do not inherit the launch RPC's trace lifetime."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar

from opentelemetry.context import attach, detach

from theater.daemon.harness_runtime.manager import HarnessRuntimeManager, ManagedRuntime
from theater.observability.tracing import extract_trace_context, inject_trace_context


async def test_health_monitor_starts_outside_request_trace(monkeypatch):
    inherited = ContextVar("unrelated", default="empty")
    inherited.set("preserved")
    observed = asyncio.Future()
    manager = HarnessRuntimeManager()

    async def monitor(*_):
        observed.set_result((inject_trace_context(), inherited.get()))

    monkeypatch.setattr(manager, "_monitor_health", monitor)
    manager._recovery_callback = lambda *_: None
    parent = {"traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01"}
    token = attach(extract_trace_context(parent))
    try:
        manager._ensure_monitor(ManagedRuntime("test", runtime=object()), 1)
        assert await observed == ({}, "preserved")
        assert inject_trace_context() == parent
    finally:
        detach(token)
        await manager.aclose()
