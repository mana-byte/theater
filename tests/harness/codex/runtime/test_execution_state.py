"""Codex native execution state follows the connection and native evidence."""

from __future__ import annotations

import asyncio

from tests.support.codex_native_items import completed_item
from tests.support.codex_server import (
    ScriptedCodexIO,
    ScriptedCodexServer,
    make_runtime,
    open_new,
    summary_turn_completed,
    thread_started,
    thread_status_changed,
)
from tests.support.waiting import wait_until
from theater.harness.builtin.plugins.codex import runtime_constants as codex_runtime_constants
from theater.harness.contracts.channels import ChannelHealthState
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    RuntimeExecutionState,
    RuntimeNotification,
    SessionOpenMode,
)


async def test_execution_state_lifecycle_is_native_derived_only() -> None:
    server = ScriptedCodexServer()
    runtime = make_runtime(server)
    # Before any native session exists, nothing is confirmed: UNKNOWN is
    # never proof of idle.
    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.UNKNOWN
    server.push(thread_started())
    await runtime.open_session(mode=SessionOpenMode.NEW)
    # The thread/started broadcast carried the backend's exact idle status
    # on a bound, connected session.
    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.IDLE
    # thread/status/changed(active) with no turn/started is still the
    # backend's exact active fact: ACTIVE.
    server.push(thread_status_changed("active"))
    await wait_until(lambda: runtime._thread_status == "active", what="active status broadcast")
    snapshot = await runtime.snapshot()
    assert snapshot.execution_state is RuntimeExecutionState.ACTIVE
    assert snapshot.native_turn_id is None
    server.push(thread_status_changed("idle"))
    await wait_until(lambda: runtime._thread_status == "idle", what="idle status broadcast")
    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.IDLE
    # turn/started is the native active fact as well.
    server.push(
        RuntimeNotification(
            method="turn/started",
            params={"threadId": "ui-thread-1", "turn": {"id": "turn-1"}},
        )
    )
    await wait_until(lambda: runtime._active_turn_id == "turn-1", what="turn 1 to start")
    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.ACTIVE
    # The turn completed; the thread-status broadcast has not arrived yet, so
    # the last native fact is still active — no heuristic idle.
    server.push(
        summary_turn_completed(
            turn_id="turn-1",
            items_view="full",
            items=[{"id": "i-agent", "type": "agentMessage", "text": "final answer"}],
        )
    )
    await wait_until(lambda: not server.pending, what="turn completion consumed")
    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.ACTIVE
    # An unrecognized thread status is never idle: UNKNOWN.
    server.push(thread_status_changed("paused"))
    await wait_until(lambda: runtime._thread_status == "paused", what="unrecognized status")
    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.UNKNOWN
    await runtime.aclose()


async def test_execution_state_idle_requires_bound_live_connection() -> None:
    server = ScriptedCodexServer()
    io = ScriptedCodexIO(server)
    runtime, _binding = await open_new(server, io=io)
    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.IDLE
    # The notification stream ends: the exact idle status is no longer
    # confirmed on a live connection — UNKNOWN, never idle.
    assert io.connection is not None
    io.connection.closed = True
    for _ in range(400):
        if (await runtime.snapshot()).health is ConnectionHealth.DISCONNECTED:
            break
        await asyncio.sleep(0.005)
    snapshot = await runtime.snapshot()
    assert snapshot.health is ConnectionHealth.DISCONNECTED
    assert snapshot.execution_state is RuntimeExecutionState.UNKNOWN
    await runtime.aclose()


async def test_execution_state_active_is_still_known_after_disconnect() -> None:
    server = ScriptedCodexServer()
    io = ScriptedCodexIO(server)
    runtime, _binding = await open_new(server, io=io)
    server.push(
        RuntimeNotification(
            method="turn/started",
            params={"threadId": "ui-thread-1", "turn": {"id": "turn-1"}},
        )
    )
    await wait_until(lambda: runtime._active_turn_id == "turn-1", what="turn 1 to start")
    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.ACTIVE
    # A dropped connection does not erase the still-known active turn: the
    # state stays ACTIVE across the disconnect; the daemon decides recovery.
    assert io.connection is not None
    io.connection.closed = True
    for _ in range(400):
        if (await runtime.snapshot()).health is ConnectionHealth.DISCONNECTED:
            break
        await asyncio.sleep(0.005)
    snapshot = await runtime.snapshot()
    assert snapshot.health is ConnectionHealth.DISCONNECTED
    assert snapshot.native_turn_id == "turn-1"
    assert snapshot.execution_state is RuntimeExecutionState.ACTIVE
    await runtime.aclose()


async def test_stream_end_marks_disconnected_wakes_readers_and_never_reconnects() -> None:
    server = ScriptedCodexServer()
    io = ScriptedCodexIO(server)
    runtime, _binding = await open_new(server, io=io)
    source = runtime.live_source()
    wakes: list[str] = []
    source.set_activity_callback(lambda: wakes.append("wake"))
    assert io.connection is not None
    io.connection.closed = True
    # The health transition itself wakes the observer state waiter so a
    # blocked reader sees the disconnect promptly.
    for _ in range(400):
        if (await runtime.snapshot()).health is ConnectionHealth.DISCONNECTED:
            break
        await asyncio.sleep(0.005)
    snapshot = await runtime.snapshot()
    assert snapshot.health is ConnectionHealth.DISCONNECTED
    assert snapshot.execution_state is RuntimeExecutionState.UNKNOWN
    assert wakes
    # No reconnect from the plugin: the same single connection, and no
    # second initialize handshake.
    assert server.connect_count == 1
    assert [method for method, _ in server.requests if method == "initialize"] == ["initialize"]
    await runtime.aclose()


async def test_buffer_overflow_degrade_wakes_readers() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    wakes: list[str] = []
    source.set_activity_callback(lambda: wakes.append("wake"))
    total = codex_runtime_constants.CODEX_RUNTIME_EVENTS_BUFFER + 10
    for index in range(total):
        server.push(completed_item(f"i-{index}", item_type="agentMessage", text=f"m{index}"))
    await wait_until(
        lambda: len(wakes) >= total and source.health_snapshot()[0].dropped > 0,
        what="every buffered push to wake the reader",
    )
    await source.read()
    health = source.health_snapshot()[0]
    assert health.state is ChannelHealthState.DEGRADED
    assert health.dropped > 0
    # Every bounded-buffer push wakes the reader — the overflow transition
    # is visible state, not a silent stall.
    assert len(wakes) >= total
    await runtime.aclose()
