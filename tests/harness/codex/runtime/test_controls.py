"""Codex native runtime send, steer and interrupt delivery outcomes."""

from __future__ import annotations

import asyncio

from tests.support.codex_server import (
    ScriptedCodexServer,
    open_new,
)
from tests.support.waiting import wait_until
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    DeliveryResult,
    RuntimeExecutionState,
    RuntimeNotification,
    RuntimeRequestError,
    RuntimeRequestTimeout,
)


async def test_send_starts_turn_and_subscribes_after_rollout() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    receipt = await runtime.send(operation_id="op-1", prompt="do the thing")
    assert receipt.operation_id == "op-1"
    assert receipt.result is DeliveryResult.ACCEPTED
    assert receipt.native_turn_id == "turn-1"
    started = server.requested("turn/start")
    assert started == [
        {
            "threadId": "ui-thread-1",
            "input": [{"type": "text", "text": "do the thing"}],
            "clientUserMessageId": "op-1",
        }
    ]
    # The rollout materializes with the accepted turn: subscription follows.
    assert server.requested("thread/resume") == [
        {
            "threadId": "ui-thread-1",
            "excludeTurns": True,
            "initialTurnsPage": {"limit": 2, "itemsView": "summary", "sortDirection": "desc"},
        }
    ]
    snapshot = await runtime.snapshot()
    assert snapshot.native_turn_id == "turn-1"
    await runtime.aclose()


async def test_send_reports_actual_returned_turn_on_busy_race() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    # A simultaneous native-UI submission absorbed the message: turn/start
    # returns the SAME active turn instead of creating a new one.
    server.respond("turn/start", {"turn": {"id": "ui-turn-active", "status": "inProgress"}})
    server.respond(
        "thread/resume",
        {
            "thread": {
                "id": "ui-thread-1",
                "status": {"type": "active"},
                "turns": [{"id": "ui-turn-active", "status": "inProgress", "items": []}],
            }
        },
    )
    receipt = await runtime.send(operation_id="op-2", prompt="overlapping")
    assert receipt.result is DeliveryResult.ACCEPTED
    assert receipt.native_turn_id == "ui-turn-active"
    snapshot = await runtime.snapshot()
    assert snapshot.native_turn_id == "ui-turn-active"
    await runtime.aclose()


async def test_send_never_fabricates_turn_identity_from_malformed_result() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    server.respond("turn/start", {"turn": {"items": []}})
    receipt = await runtime.send(operation_id="op-3", prompt="anything")
    assert receipt.result is DeliveryResult.UNKNOWN
    assert receipt.error_code == "malformed_turn_start_result"
    await runtime.aclose()


async def test_send_rejected_and_unknown_paths() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    server.fail("turn/start", RuntimeRequestError(-32000, "thread is archived"))
    rejected = await runtime.send(operation_id="op-4", prompt="x")
    assert rejected.result is DeliveryResult.REJECTED
    assert rejected.error_code == "turn_start_refused"
    assert "archived" in (rejected.error or "")

    server.fail("turn/start", RuntimeRequestTimeout())
    unknown = await runtime.send(operation_id="op-5", prompt="x")
    assert unknown.result is DeliveryResult.UNKNOWN
    assert unknown.error_code == "control_ack_timeout"
    # An acknowledgement timeout can follow backend acceptance.
    snapshot = await runtime.snapshot()
    assert snapshot.health is ConnectionHealth.DISCONNECTED
    assert snapshot.execution_state is RuntimeExecutionState.UNKNOWN
    assert snapshot.native_turn_id is None
    await runtime.aclose()


async def test_stale_steer_refusal_stays_a_refusal() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    server.fail("turn/steer", RuntimeRequestError(-32600, "no active turn to steer"))
    receipt = await runtime.steer(operation_id="op-7", native_turn_id="turn-gone", prompt="amend")
    assert receipt.result is DeliveryResult.REJECTED
    assert receipt.error_code == "stale_turn"
    # A refusal is never reinterpreted as a send or queue operation.
    assert server.requested("turn/start") == []
    assert server.requested("thread/queue/add") == []
    await runtime.aclose()


async def test_steer_rejects_unvalidated_returned_turn_identity() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    # The verified dialect echoes the steered active turn; any other id is an
    # identity contradiction that must not be reported as ACCEPTED.
    server.respond("turn/steer", {"turnId": "turn-other"})
    receipt = await runtime.steer(
        operation_id="op-steer-x", native_turn_id="turn-1", prompt="amend"
    )
    assert receipt.result is DeliveryResult.UNKNOWN
    assert receipt.error_code == "turn_identity_mismatch"
    assert receipt.native_turn_id is None
    assert server.requested("turn/start") == []
    # A missing turnId is a malformed reply, never a fabricated acceptance.
    server.respond("turn/steer", {})
    receipt = await runtime.steer(
        operation_id="op-steer-y", native_turn_id="turn-1", prompt="amend"
    )
    assert receipt.result is DeliveryResult.UNKNOWN
    assert receipt.error_code == "malformed_turn_steer_result"
    assert receipt.native_turn_id is None
    await runtime.aclose()


async def test_interrupt_without_active_turn_is_an_honest_refusal() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    receipt = await runtime.interrupt(operation_id="op-9")
    assert receipt.result is DeliveryResult.REJECTED
    assert receipt.error_code == "no_active_turn"
    assert server.requested("turn/interrupt") == []
    await runtime.aclose()


async def test_interrupt_settles_from_turn_completed_before_the_ack_returns() -> None:
    """Fast settlement: the terminal notification may beat the response."""
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    server.push(
        RuntimeNotification(
            method="turn/started",
            params={"threadId": "ui-thread-1", "turn": {"id": "turn-1", "status": "inProgress"}},
        )
    )
    await wait_until(lambda: runtime._active_turn_id == "turn-1", what="turn 1 to start")
    gate = asyncio.Event()
    server.request_gates["turn/interrupt"] = gate
    entered = server.request_entered.setdefault("turn/interrupt", asyncio.Event())
    interrupt_task = asyncio.create_task(runtime.interrupt(operation_id="op-fast"))
    await asyncio.wait_for(entered.wait(), timeout=5)

    # The backend reports turn/completed (interrupted) while the interrupt
    # request is still outstanding: settlement must not wait for the ack.
    server.push(
        RuntimeNotification(
            method="turn/completed",
            params={
                "threadId": "ui-thread-1",
                "turn": {
                    "id": "turn-1",
                    "status": "interrupted",
                    "items": [],
                    "itemsView": "full",
                },
            },
        )
    )
    await wait_until(
        lambda: len(source.buffered_terminal_evidence()) == 1, what="interrupted outcome to land"
    )
    outcomes = (await source.read()).terminal_evidence
    assert [outcome.terminal.value for outcome in outcomes] == ["interrupted"]
    assert [outcome.native_turn_id for outcome in outcomes] == ["turn-1"]

    gate.set()
    receipt = await asyncio.wait_for(interrupt_task, timeout=5)
    assert receipt.result is DeliveryResult.ACCEPTED
    assert receipt.native_turn_id == "turn-1"
    snapshot = await runtime.snapshot()
    assert snapshot.native_turn_id is None
    # The already-delivered terminal never repeats after the ack returns.
    assert (await source.read()).terminal_evidence == ()
    await runtime.aclose()
