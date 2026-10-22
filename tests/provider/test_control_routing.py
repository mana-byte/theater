"""Provider-routed send, queue, steer and interrupt keep durable linkage and terminal fences."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import select, update

from tests.support.builders import operator_context
from tests.support.provider_controls import online, provider_target
from tests.support.provider_native import assert_result_valid
from tests.support.queued_followups import provider_terminal_op
from tests.support.waiting import settle_operations
from theater.daemon.frontend.control_handlers import (
    _error,
    controls_interrupt,
    controls_queue_followup,
    controls_send,
    controls_settings_update,
    controls_steer,
)
from theater.daemon.frontend.operation_handlers import operations_reconcile
from theater.daemon.schema import orchestration_events, terminal_bindings
from theater.daemon.terminals import CallbackOutcomeUnknown
from theater.frontend.schemas import validate_callback_request
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
)
from theater.models import (
    BadRequest,
    JobState,
    PublicOperationState,
    Status,
    now,
)


async def _control_for_public_operation(daemon, operation_id: str):
    for _ in range(20):
        operation = daemon.operation_service.get(operation_id)
        if operation.control_operation_id is not None:
            return daemon.store.get_control_operation(operation.control_operation_id)
        await asyncio.sleep(0)
    return None


async def test_provider_send_links_one_public_operation_job_and_control(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)
    requests: list[dict[str, object]] = []

    async def request(_provider, generation, method, params):
        frame = {"type": "request", "id": "callback-a", "method": method, "params": params}
        validate_callback_request(frame)
        requests.append(dict(params))
        return {
            "operation_id": params["operation_id"],
            "provider_generation": generation,
            "terminal_id": params["terminal_id"],
            "terminal_incarnation": params["terminal_incarnation"],
            "delivery": "accepted",
        }

    monkeypatch.setattr(daemon.terminal_service.connections, "request", request)
    params = {
        "participant_id": participant_id,
        "prompt": "review this",
        "response_format": {"type": "object"},
    }
    accepted = await controls_send(
        daemon, operator_context(), params, idempotency_key="provider-send-a"
    )
    duplicate = await controls_send(
        daemon, operator_context(), params, idempotency_key="provider-send-a"
    )
    assert_result_valid("frontend.controls.send", accepted)
    assert duplicate == accepted
    await settle_operations(daemon)

    operation = daemon.operation_service.get(accepted["operation_id"])
    assert operation.state == PublicOperationState.SUCCEEDED.value, operation.error
    assert operation.control_operation_id == f"{operation.operation_id}:control"
    assert operation.job_handle is not None
    job = daemon.store.get_job(operation.job_handle)
    assert job.response_format == '{"type":"object"}'
    assert job.caller_id == "cli"
    assert job.actor_client_id == "operator-a"
    assert job.actor_participant_id is None
    control = daemon.store.get_control_operation(operation.control_operation_id)
    assert control is not None
    assert control.transport is ControlTransport.PROVIDER_TERMINAL
    assert (control.provider_id, control.provider_generation) == ("provider-a", 1)
    assert (control.terminal_id, control.terminal_incarnation) == (
        "terminal-a",
        "incarnation-a",
    )
    assert requests == [
        {
            "operation_id": operation.operation_id,
            "provider_generation": 1,
            "participant_id": participant_id,
            "terminal_id": "terminal-a",
            "terminal_incarnation": "incarnation-a",
            "expected_occupant": "occupant-a",
            "action": {
                "kind": "submit_text",
                "text": (
                    "Return your final answer as a single bare JSON value (no code fences, "
                    'no prose) matching this schema hint: {"type":"object"}\n\nreview this'
                ),
            },
            "require_absent": True,
        }
    ]


async def test_second_provider_send_is_refused_while_first_job_is_running(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)
    callback_count = 0

    async def accepted(_provider, generation, _method, params):
        nonlocal callback_count
        callback_count += 1
        return {
            "operation_id": params["operation_id"],
            "provider_generation": generation,
            "terminal_id": params["terminal_id"],
            "terminal_incarnation": params["terminal_incarnation"],
            "delivery": "accepted",
        }

    monkeypatch.setattr(daemon.terminal_service.connections, "request", accepted)
    first = await controls_send(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "prompt": "first"},
        idempotency_key="provider-send-first",
    )
    await settle_operations(daemon)
    assert daemon.operation_service.get(first["operation_id"]).state == "succeeded"

    second = await controls_send(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "prompt": "second"},
        idempotency_key="provider-send-second",
    )
    await settle_operations(daemon)

    refused = daemon.operation_service.get(second["operation_id"])
    assert refused.state == PublicOperationState.FAILED.value
    assert refused.error is not None and refused.error["code"] == "busy"
    assert callback_count == 1


async def test_historical_unpinned_queue_refuses_after_route_selection(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)
    job = daemon.jobs.create(
        handle="historical#queue",
        caller_id="cli",
        target_id=participant_id,
        kind="send",
        prompt="old queued prompt",
        cwd=None,
    )
    operation_id = "historical#queue:followup"
    daemon.store.reserve_control_operation(
        provider_terminal_op(
            operation_id,
            participant_id,
            kind=ControlKind.QUEUE_FOLLOWUP,
            phase=ControlDeliveryPhase.QUEUED,
            job_handle=job.handle,
        )
    )

    async def no_delivery(*_args):
        pytest.fail("a row without a pinned provider must not use the current provider")

    monkeypatch.setattr(daemon.terminal_service.connections, "request", no_delivery)
    absence_checks = 0
    original_require_absent = daemon.controls._gates.require_absent

    async def require_absent(participant_id: str) -> None:
        nonlocal absence_checks
        absence_checks += 1
        await original_require_absent(participant_id)

    daemon.controls._gates = replace(daemon.controls._gates, require_absent=require_absent)
    outcome = await daemon.controls.dispatch_queue(participant_id)

    assert outcome.failed == ((job.handle, "not_addressable"),)
    assert absence_checks == 2
    control = daemon.store.get_control_operation(operation_id)
    assert control is not None
    assert control.transport is ControlTransport.TERMINAL_FALLBACK
    assert control.delivery_phase is ControlDeliveryPhase.SETTLED
    assert control.delivery_result is DeliveryResult.REJECTED
    assert control.error == f"participant {participant_id!r} has no current terminal-provider route"
    assert daemon.store.get_job(job.handle).state == JobState.CRASHED


async def test_pre_reserved_provider_send_rechecks_busy_after_modal_gate(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)

    async def become_working(_participant_id: str) -> None:
        daemon.registry.set_status(participant_id, Status.WORKING)

    async def no_delivery(*_args):
        pytest.fail("pre-reserved send must refuse before provider delivery")

    daemon.controls._gates = replace(daemon.controls._gates, provider_modal_check=become_working)
    monkeypatch.setattr(daemon.terminal_service.connections, "request", no_delivery)
    accepted = await controls_send(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "prompt": "reserved"},
        idempotency_key="provider-pre-reserved-busy",
    )
    await settle_operations(daemon)

    operation = daemon.operation_service.get(accepted["operation_id"])
    assert operation.job_handle is not None
    assert operation.state == PublicOperationState.FAILED.value
    assert operation.error is not None and operation.error["code"] == "busy"


async def test_public_send_acceptance_atomically_persists_job_control_and_operation(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)
    started: list[str] = []

    def suppress_start(operation_id, *, dispatch, side_effect):
        del dispatch, side_effect
        started.append(operation_id)

    monkeypatch.setattr(daemon.operation_service, "start", suppress_start)
    before = daemon.store.journal.current_sequence()
    params = {"participant_id": participant_id, "prompt": "persist first"}

    accepted = await controls_send(
        daemon,
        operator_context(),
        params,
        idempotency_key="atomic-public-send",
    )
    duplicate = await controls_send(
        daemon,
        operator_context(),
        params,
        idempotency_key="atomic-public-send",
    )

    operation = daemon.operation_service.get(accepted["operation_id"])
    control = daemon.store.get_control_operation(operation.control_operation_id)
    job = daemon.store.get_job(operation.job_handle)
    assert duplicate == accepted
    assert accepted["job_handle"] == operation.job_handle
    assert operation.state == PublicOperationState.ACCEPTED.value
    assert operation.phase == "control_reserved"
    assert control is not None and control.delivery_phase is ControlDeliveryPhase.RESERVED
    assert job.state == JobState.RUNNING
    assert started == [operation.operation_id]
    groups = daemon.store.journal.groups_after(before, limit=10)
    group = next(
        item
        for item in groups
        if any(event.entity_id == operation.operation_id for event in item.events)
    )
    assert [event.kind for event in group.events] == [
        "job.updated",
        "participant.controls_changed",
        "operation.updated",
    ]
    assert (
        daemon.store.conn.execute(
            select(orchestration_events.c.transaction_id).where(
                orchestration_events.c.ending_sequence == group.ending_sequence
            )
        )
        .scalars()
        .all()
        == [group.transaction_id] * 3
    )


async def test_provider_unknown_keeps_barrier_and_queued_followup(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)
    callback_count = 0

    async def unknown(_provider, _generation, _method, _params):
        nonlocal callback_count
        callback_count += 1
        raise CallbackOutcomeUnknown("provider-a", "callback-a", "response_lost")

    monkeypatch.setattr(daemon.terminal_service.connections, "request", unknown)
    accepted = await controls_send(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "prompt": "uncertain"},
        idempotency_key="provider-send-unknown",
    )
    await settle_operations(daemon)
    operation = daemon.operation_service.get(accepted["operation_id"])
    assert operation.state == PublicOperationState.UNCERTAIN.value
    assert daemon.store.has_execution_barrier(participant_id)
    control = daemon.store.get_control_operation(operation.control_operation_id)
    assert control is not None
    assert control.transport is ControlTransport.PROVIDER_TERMINAL
    assert daemon.store.unresolved_prompt_delivery_operations(participant_id) == []
    reconciled = await operations_reconcile(
        daemon,
        operator_context(),
        {"operation_id": operation.operation_id},
    )
    assert reconciled["state"] == PublicOperationState.UNCERTAIN.value
    assert callback_count == 1

    second = await controls_send(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "prompt": "must not cross the barrier"},
        idempotency_key="provider-send-after-unknown",
    )
    await settle_operations(daemon)
    refused = daemon.operation_service.get(second["operation_id"])
    assert refused.state == PublicOperationState.FAILED.value
    assert refused.error is not None and refused.error["code"] == "busy"
    assert callback_count == 1

    queued = await controls_queue_followup(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "prompt": "later"},
        idempotency_key="provider-queue-a",
    )
    control = await _control_for_public_operation(daemon, queued["operation_id"])
    await asyncio.sleep(0)
    queued_operation = daemon.operation_service.get(queued["operation_id"])
    assert queued_operation.state == PublicOperationState.RUNNING.value
    assert control is not None and control.delivery_phase.value == "queued"
    assert control.transport is ControlTransport.PROVIDER_TERMINAL
    await daemon.controls.reconcile_ambiguous_delivery(participant_id, now_ts=now() + 10_000)
    assert daemon.store.get_job(operation.job_handle).state == JobState.RUNNING
    assert daemon.store.has_execution_barrier(participant_id)
    assert daemon.store.get_control_operation(control.operation_id).delivery_phase.value == "queued"

    assert daemon.controls.fail_undelivered_followups([participant_id]) == []
    preserved = daemon.store.get_control_operation(control.operation_id)
    assert preserved is not None
    assert preserved.delivery_phase is ControlDeliveryPhase.QUEUED
    assert preserved.transport is ControlTransport.PROVIDER_TERMINAL
    assert (
        preserved.provider_id,
        preserved.provider_generation,
        preserved.terminal_id,
        preserved.terminal_incarnation,
    ) == ("provider-a", 1, "terminal-a", "incarnation-a")
    monkeypatch.setattr(daemon.terminal_service.connections, "health", lambda *_: "offline")
    deferred = await daemon.controls.dispatch_queue(participant_id)
    assert deferred.deferred is True
    assert daemon.store.get_control_operation(control.operation_id).delivery_phase.value == "queued"


def test_public_error_normalization_bounds_and_discards_invalid_details() -> None:
    class InvalidError(Exception):
        code = "x" * 600

        def __init__(self, message: str) -> None:
            self.details = {"not_json": object(), "not_finite": float("nan")}
            super().__init__(message)

    error = _error(InvalidError("m" * 9_000))
    assert len(error["code"]) == 512
    assert len(error["message"]) == 8192
    assert "details" not in error


async def test_provider_queue_waits_for_accepted_job_to_finish(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)
    requests: list[str] = []

    async def accepted(_provider, generation, method, params):
        requests.append(method)
        return {
            "operation_id": params["operation_id"],
            "provider_generation": generation,
            "terminal_id": params["terminal_id"],
            "terminal_incarnation": params["terminal_incarnation"],
            "delivery": "accepted",
        }

    monkeypatch.setattr(daemon.terminal_service.connections, "request", accepted)
    sent = await controls_send(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "prompt": "start"},
        idempotency_key="provider-active-send",
    )
    await settle_operations(daemon)
    send_operation = daemon.operation_service.get(sent["operation_id"])
    queued = await controls_queue_followup(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "prompt": "later"},
        idempotency_key="provider-active-queue",
    )
    control = await _control_for_public_operation(daemon, queued["operation_id"])
    queued_operation = daemon.operation_service.get(queued["operation_id"])
    assert control is not None and control.delivery_phase.value == "queued"
    queued_job = daemon.store.get_job(queued_operation.job_handle)
    assert queued_job.actor_client_id == "operator-a"
    assert requests == ["terminal.deliver"]

    daemon.jobs.finish(send_operation.job_handle, state=JobState.DONE)
    await daemon.controls.dispatch_queue(participant_id)
    await settle_operations(daemon)
    assert daemon.operation_service.get(queued["operation_id"]).state == "succeeded"
    assert requests == ["terminal.deliver", "terminal.deliver"]


async def test_public_provider_queue_reports_presence_refusal_on_its_original_operation(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)
    calls = 0

    async def deliver(_provider, generation, _method, params):
        nonlocal calls
        calls += 1
        response = {
            "operation_id": params["operation_id"],
            "provider_generation": generation,
            "terminal_id": params["terminal_id"],
            "terminal_incarnation": params["terminal_incarnation"],
        }
        if calls == 1:
            return {**response, "delivery": "accepted"}
        return {
            **response,
            "delivery": "rejected",
            "error": {"code": "human_present", "message": "human arrived"},
        }

    monkeypatch.setattr(daemon.terminal_service.connections, "request", deliver)
    sent = await controls_send(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "prompt": "start"},
        idempotency_key="provider-public-start",
    )
    await settle_operations(daemon)
    queued = await controls_queue_followup(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "prompt": "later"},
        idempotency_key="provider-public-queue-presence",
    )
    await _control_for_public_operation(daemon, queued["operation_id"])
    first_handle = daemon.operation_service.get(sent["operation_id"]).job_handle
    daemon.jobs.finish(first_handle, state=JobState.DONE)
    await daemon.controls.dispatch_queue(participant_id)
    await settle_operations(daemon)

    operation = daemon.operation_service.get(queued["operation_id"])
    assert operation.state == "failed" and operation.control_operation_id is not None
    assert daemon.store.get_job(operation.job_handle).state == JobState.CRASHED
    assert daemon.store.queued_control_operations(participant_id) == []


async def test_queued_public_operation_waiter_is_owned_and_drained_on_close(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)

    async def accepted(_provider, generation, _method, params):
        return {
            "operation_id": params["operation_id"],
            "provider_generation": generation,
            "terminal_id": params["terminal_id"],
            "terminal_incarnation": params["terminal_incarnation"],
            "delivery": "accepted",
        }

    monkeypatch.setattr(daemon.terminal_service.connections, "request", accepted)
    sent = await controls_send(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "prompt": "active"},
        idempotency_key="provider-owned-send",
    )
    await settle_operations(daemon)
    queued = await controls_queue_followup(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "prompt": "held"},
        idempotency_key="provider-owned-queue",
    )
    await _control_for_public_operation(daemon, queued["operation_id"])
    await asyncio.sleep(0)
    assert daemon.operation_service.get(queued["operation_id"]).state == "running"
    assert daemon.operation_service.owned_tasks

    await daemon.operation_service.aclose()

    assert daemon.operation_service.owned_tasks == ()
    assert daemon.operation_service.get(queued["operation_id"]).state == "uncertain"
    assert (
        daemon.store.get_job(daemon.operation_service.get(sent["operation_id"]).job_handle).state
        == JobState.RUNNING
    )
    control = daemon.store.get_control_operation(
        daemon.operation_service.get(queued["operation_id"]).control_operation_id
    )
    assert control is not None and control.delivery_phase is ControlDeliveryPhase.QUEUED


async def test_provider_rejection_is_definitive_and_provider_settings_stay_unsupported(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)
    methods: list[str] = []

    async def rejected(_provider, generation, method, params):
        methods.append(method)
        return {
            "operation_id": params["operation_id"],
            "provider_generation": generation,
            "terminal_id": params["terminal_id"],
            "terminal_incarnation": params["terminal_incarnation"],
            "delivery": "rejected",
            "error": {"code": "provider_busy", "message": "terminal refused input"},
        }

    monkeypatch.setattr(daemon.terminal_service.connections, "request", rejected)
    sent = await controls_send(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "prompt": "refuse"},
        idempotency_key="provider-send-rejected",
    )
    await settle_operations(daemon)
    operation = daemon.operation_service.get(sent["operation_id"])
    assert operation.state == PublicOperationState.FAILED.value
    assert operation.control_operation_id is not None
    assert daemon.store.has_execution_barrier(participant_id) is False
    assert daemon.store.get_job(operation.job_handle).state == JobState.CRASHED

    settings = await controls_settings_update(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "model": "gpt-5"},
        idempotency_key="provider-settings-refused",
    )
    assert_result_valid("frontend.controls.settings.update", settings)
    await settle_operations(daemon)
    settings_operation = daemon.operation_service.get(settings["operation_id"])
    assert settings_operation.state == PublicOperationState.FAILED.value
    assert settings_operation.control_operation_id is None
    assert methods == ["terminal.deliver"]


# Vibe's Enter queues the text as a separate native turn: it must never pass for a steer.
@pytest.mark.parametrize(
    ("harness", "steers"), [("codex", True), ("claude", False), ("vibe", False)]
)
async def test_provider_steer_submits_with_enter_only_where_the_harness_declares_it(
    daemon, monkeypatch: pytest.MonkeyPatch, harness: str, steers: bool
) -> None:
    online(monkeypatch, daemon)
    actions: list[str] = []

    async def accepted(_provider, generation, method, params):
        if method == "terminal.deliver":
            actions.append(params["action"]["kind"])
        return {
            "operation_id": params["operation_id"],
            "provider_generation": generation,
            "terminal_id": params["terminal_id"],
            "terminal_incarnation": params["terminal_incarnation"],
            "delivery": "accepted",
        }

    monkeypatch.setattr(daemon.terminal_service.connections, "request", accepted)
    participant_id = provider_target(daemon, harness=harness)
    await controls_send(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "prompt": "start"},
        idempotency_key="steer-enter-send",
    )
    await settle_operations(daemon)
    actions.clear()
    steer = controls_steer(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "prompt": "adjust"},
        idempotency_key="steer-enter",
    )
    if not steers:
        with pytest.raises(BadRequest, match="does not offer a transport for steer"):
            await steer
        assert actions == []
        return
    assert_result_valid("frontend.controls.steer", await steer)
    await settle_operations(daemon)
    assert actions == ["submit_text"]  # Enter, never a bare paste


async def test_provider_steer_and_interrupt_use_existing_job_and_exact_fence(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)
    methods: list[str] = []

    async def accepted(_provider, generation, method, params):
        methods.append(method)
        return {
            "operation_id": params["operation_id"],
            "provider_generation": generation,
            "terminal_id": params["terminal_id"],
            "terminal_incarnation": params["terminal_incarnation"],
            "delivery": "accepted",
        }

    monkeypatch.setattr(daemon.terminal_service.connections, "request", accepted)
    sent = await controls_send(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "prompt": "start"},
        idempotency_key="provider-send-running",
    )
    await settle_operations(daemon)
    send_operation = daemon.operation_service.get(sent["operation_id"])
    steered = await controls_steer(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "prompt": "adjust"},
        idempotency_key="provider-steer-a",
    )
    assert_result_valid("frontend.controls.steer", steered)
    steer_reserved = daemon.operation_service.get(steered["operation_id"])
    assert steer_reserved.state == PublicOperationState.ACCEPTED.value
    assert steer_reserved.job_handle == send_operation.job_handle
    assert daemon.store.get_control_operation(steer_reserved.control_operation_id) is not None
    await settle_operations(daemon)
    steer_operation = daemon.operation_service.get(steered["operation_id"])
    assert steer_operation.job_handle == send_operation.job_handle
    interrupted = await controls_interrupt(
        daemon,
        operator_context(),
        {"participant_id": participant_id},
        idempotency_key="provider-interrupt-a",
    )
    assert_result_valid("frontend.controls.interrupt", interrupted)
    interrupt_reserved = daemon.operation_service.get(interrupted["operation_id"])
    assert interrupt_reserved.state == PublicOperationState.ACCEPTED.value
    assert interrupt_reserved.control_operation_id is not None
    assert interrupt_reserved.job_handle is None
    await settle_operations(daemon)
    assert daemon.operation_service.get(interrupted["operation_id"]).state == "succeeded"
    daemon.jobs.finish(send_operation.job_handle, state=JobState.DONE)
    queued = await controls_queue_followup(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "prompt": "next"},
        idempotency_key="provider-queue-dispatch",
    )
    assert_result_valid("frontend.controls.queue_followup", queued)
    queue_reserved = daemon.operation_service.get(queued["operation_id"])
    assert queue_reserved.state == PublicOperationState.ACCEPTED.value
    assert queue_reserved.control_operation_id is not None
    assert queued["job_handle"] == queue_reserved.job_handle
    await settle_operations(daemon)
    assert daemon.operation_service.get(queued["operation_id"]).state == "succeeded"
    assert methods == [
        "terminal.deliver",
        "terminal.deliver",
        "terminal.interrupt",
        "terminal.deliver",
    ]


@pytest.mark.parametrize("harness", ["claude", "codex", "opencode", "pi", "vibe"])
async def test_provider_interrupt_is_idle_noop_and_uses_each_harness_plan(
    daemon, monkeypatch, harness
):
    participant_id = provider_target(daemon, harness=harness)
    online(monkeypatch, daemon)
    actions = []

    async def accepted(_provider, generation, method, params):
        validate_callback_request(
            {"type": "request", "id": "callback-a", "method": method, "params": params}
        )
        actions.append(params["action"])
        return {
            "operation_id": params["operation_id"],
            "provider_generation": generation,
            "terminal_id": params["terminal_id"],
            "terminal_incarnation": params["terminal_incarnation"],
            "delivery": "accepted",
        }

    monkeypatch.setattr(daemon.terminal_service.connections, "request", accepted)
    idle = await controls_interrupt(
        daemon,
        operator_context(),
        {"participant_id": participant_id},
        idempotency_key="idle-interrupt",
    )
    await settle_operations(daemon)
    operation = daemon.operation_service.get(idle["operation_id"])
    assert operation.state == "succeeded"
    assert operation.phase == "already_idle"
    assert operation.result == {"interrupted": False}
    assert not actions

    daemon.store.set_status(participant_id, Status.WORKING)
    outcome = await daemon.controls.interrupt(participant_id, caller_id="cli")
    assert outcome.interrupted
    assert actions == [
        {
            "keys": ["Escape", "Escape"] if harness == "opencode" else ["Escape"],
            "inter_key_delay_seconds": 0.05 if harness == "opencode" else 0.0,
        }
    ]
    assert daemon.registry.get(participant_id).status is Status.WORKING


@pytest.mark.parametrize("changed", ["generation", "incarnation"])
async def test_provider_identity_change_refuses_without_dispatch(
    daemon, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)
    called = False

    async def request(*_args):
        nonlocal called
        called = True
        raise AssertionError("stale public dispatch must not reach the provider")

    monkeypatch.setattr(daemon.terminal_service.connections, "request", request)
    accepted = await controls_send(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "prompt": "fenced"},
        idempotency_key="provider-send-fenced",
    )
    with daemon.store.write_unit() as unit:
        if changed == "generation":
            assert daemon.store.terminal_bindings.restore_generation(
                participant_id,
                previous_generation=1,
                provider_generation=2,
                report_revision=2,
                health="healthy",
                updated_at=now(),
                connection=unit.connection,
            )
        else:
            unit.connection.execute(
                update(terminal_bindings)
                .where(terminal_bindings.c.participant_id == participant_id)
                .values(terminal_incarnation="incarnation-b", updated_at=now())
            )
    await settle_operations(daemon)
    assert not called
    assert daemon.operation_service.get(accepted["operation_id"]).state == "failed"
