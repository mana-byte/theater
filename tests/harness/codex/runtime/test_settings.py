"""Codex native runtime settings updates are gated, idle-only and read back."""

from __future__ import annotations

from tests.support.codex_server import (
    ScriptedCodexServer,
    make_runtime,
    open_new,
    thread_started,
)
from tests.support.waiting import wait_until
from theater.harness.contracts.channels import ChannelHealthState
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ConnectionHealth,
    DeliveryResult,
    RuntimeNotification,
    RuntimeRequestError,
    SessionOpenMode,
)


async def test_settings_update_is_gated_supplied_fields_only_and_read_back() -> None:
    from theater.harness.contracts.runtime import RuntimeCapability, RuntimeSettingField

    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    snapshot = await runtime.snapshot()
    assert snapshot.capabilities.supports(RuntimeCapability.SETTINGS_UPDATE) is True
    assert snapshot.settings.supported_fields == frozenset(RuntimeSettingField)
    server.respond(
        "thread/read",
        {"thread": {"id": "ui-thread-1", "model": "gpt-5.2", "reasoningEffort": "high"}},
    )
    receipt = await runtime.update_settings(operation_id="op-10", reasoning_effort="high")
    assert receipt.result is DeliveryResult.ACCEPTED
    assert server.requested("thread/settings/update")[-1] == {
        "threadId": "ui-thread-1",
        "effort": "high",
    }
    snapshot = await runtime.snapshot()
    assert snapshot.settings.reasoning_effort == "high"
    await runtime.aclose()


async def test_settings_gate_failure_reports_explicit_unavailable_reason() -> None:
    server = ScriptedCodexServer()
    server.fail(
        "thread/settings/update",
        RuntimeRequestError(-32600, "thread/settings/update requires experimentalApi capability"),
    )
    runtime = make_runtime(server)
    server.push(thread_started())
    await runtime.open_session(mode=SessionOpenMode.NEW)
    snapshot = await runtime.snapshot()
    from theater.harness.contracts.runtime import RuntimeCapability

    assert snapshot.capabilities.supports(RuntimeCapability.SETTINGS_UPDATE) is False
    assert (
        snapshot.capabilities.reason_for(RuntimeCapability.SETTINGS_UPDATE)
        is CapabilityUnavailableReason.GATED_BY_BACKEND
    )
    receipt = await runtime.update_settings(operation_id="op-11", model="gpt-5.2")
    assert receipt.result is DeliveryResult.REJECTED
    assert receipt.error_code == "settings_unavailable"
    await runtime.aclose()


async def test_settings_are_idle_only() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    server.push(
        RuntimeNotification(
            method="turn/started",
            params={"threadId": "ui-thread-1", "turn": {"id": "turn-5", "status": "inProgress"}},
        )
    )
    await wait_until(lambda: runtime._active_turn_id == "turn-5", what="turn 5 to start")
    receipt = await runtime.update_settings(operation_id="op-12", model="gpt-5.2")
    assert receipt.result is DeliveryResult.REJECTED
    assert receipt.error_code == "session_busy"
    await runtime.aclose()


async def test_settings_require_supplied_fields() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    receipt = await runtime.update_settings(operation_id="op-13")
    assert receipt.result is DeliveryResult.REJECTED
    assert receipt.error_code == "no_settings_fields"
    await runtime.aclose()


async def test_settings_update_with_unconfirmed_readback_exposes_uncertainty() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    # The backend accepts the update but the native readback cannot confirm
    # the effective settings.
    server.fail("thread/read", RuntimeRequestError(-32000, "read failed"))
    receipt = await runtime.update_settings(operation_id="op-unconfirmed", model="gpt-5.2")
    assert receipt.result is DeliveryResult.UNKNOWN
    assert receipt.error_code == "settings_unconfirmed"
    snapshot = await runtime.snapshot()
    assert snapshot.health is ConnectionHealth.DEGRADED
    assert any("unconfirmed" in note for note in snapshot.health_diagnostics)
    assert snapshot.settings.model is None
    health = runtime.live_source().health_snapshot()[0]
    assert health.state is ChannelHealthState.DEGRADED
    await runtime.aclose()


async def test_settings_readback_confirms_exactly_the_requested_fields() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    # A readback carrying only an unrequested field never confirms a requested one.
    server.respond("thread/read", {"thread": {"id": "ui-thread-1", "reasoningEffort": "high"}})
    receipt = await runtime.update_settings(operation_id="op-missing", model="gpt-5.2")
    assert receipt.result is DeliveryResult.UNKNOWN
    assert receipt.error_code == "settings_unconfirmed"
    snapshot = await runtime.snapshot()
    assert snapshot.settings.model is None
    assert snapshot.settings.reasoning_effort == "high"
    # A readback reporting a different value is a contradiction, not acceptance.
    server.respond("thread/read", {"thread": {"id": "ui-thread-1", "model": "gpt-4.1-mini"}})
    receipt = await runtime.update_settings(
        operation_id="op-contradicted", model="gpt-5.2", reasoning_effort="low"
    )
    assert receipt.result is DeliveryResult.UNKNOWN
    assert receipt.error_code == "settings_contradicted"
    # The readable readback's effective values are still adopted.
    snapshot = await runtime.snapshot()
    assert snapshot.settings.model == "gpt-4.1-mini"
    await runtime.aclose()


async def test_external_own_thread_settings_update_adopts_into_snapshot() -> None:
    """A UI-initiated settings change is readable without any Theater request."""
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    snapshot = await runtime.snapshot()
    assert snapshot.settings.model is None
    assert snapshot.settings.reasoning_effort is None

    # thread/settings/updated arrives unprompted: the native UI changed the
    # thread's settings on our own thread, and Theater's snapshot must adopt it.
    server.push(
        RuntimeNotification(
            method="thread/settings/updated",
            params={
                "threadId": "ui-thread-1",
                "threadSettings": {"model": "gpt-5.6-sol", "reasoningEffort": "high"},
            },
        )
    )
    await wait_until(
        lambda: (
            runtime._settings.model == "gpt-5.6-sol"
            and runtime._settings.reasoning_effort == "high"
        ),
        what="own-thread settings adopted",
    )
    snapshot = await runtime.snapshot()
    assert snapshot.settings.model == "gpt-5.6-sol"
    assert snapshot.settings.reasoning_effort == "high"

    # A foreign thread's settings change never leaks into this runtime.
    server.push(
        RuntimeNotification(
            method="thread/settings/updated",
            params={
                "threadId": "other-thread",
                "threadSettings": {"model": "gpt-4.1", "reasoningEffort": "low"},
            },
        )
    )
    await wait_until(lambda: not server.pending, what="foreign settings update consumed")
    snapshot = await runtime.snapshot()
    assert snapshot.settings.model == "gpt-5.6-sol"
    assert snapshot.settings.reasoning_effort == "high"
    await runtime.aclose()


async def test_queue_followup_capability_reports_theater_policy() -> None:
    from theater.harness.contracts.runtime import RuntimeCapability

    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    snapshot = await runtime.snapshot()
    assert snapshot.capabilities.supports(RuntimeCapability.QUEUE_FOLLOWUP) is False
    assert (
        snapshot.capabilities.reason_for(RuntimeCapability.QUEUE_FOLLOWUP)
        is CapabilityUnavailableReason.THEATER_POLICY
    )
    await runtime.aclose()


async def test_native_queue_methods_are_never_used() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    await runtime.send(operation_id="op-14", prompt="x")
    await runtime.steer(operation_id="op-15", native_turn_id="turn-1", prompt="y")
    await runtime.interrupt(operation_id="op-16", native_turn_id="turn-1")
    await runtime.update_settings(operation_id="op-17", model="gpt-5.2")
    for method, _params in server.requests:
        assert not method.startswith("thread/queue"), method
    await runtime.aclose()
    assert server.close_count == 1
    assert server.backend_alive is True
