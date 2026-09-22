from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest
from regie.controllers.actions import ActionRecord, ActionState, OperationController
from regie.controllers.controls import describe_action, format_controls_report

from theater.frontend import (
    AcceptedOperation,
    FrontendClient,
    RequestUncertain,
    ResponseValidationError,
)
from theater.frontend import Controls as PublicControls


class Controls:
    def __init__(self) -> None:
        self.keys: list[str] = []
        self.uncertain = False

    async def send(self, participant_id: str, prompt: str, *, idempotency_key: str) -> object:
        self.keys.append(idempotency_key)
        if self.uncertain:
            raise RequestUncertain("frontend.controls.send", len(self.keys))
        return SimpleNamespace(
            value=AcceptedOperation(
                operation_id="operation-a",
                state="accepted",
                participant_id=participant_id,
            )
        )


class Operations:
    async def wait(self, operation_id: str, *, wait_seconds: int) -> object:
        assert (operation_id, wait_seconds) == ("operation-a", 30)
        return SimpleNamespace(
            value=SimpleNamespace(
                timed_out=True,
                operation=SimpleNamespace(state="running", error=None),
            )
        )

    async def get(self, operation_id: str) -> object:
        assert operation_id == "operation-a"
        return SimpleNamespace(value=SimpleNamespace(state="succeeded", error=None))


class Client:
    def __init__(self) -> None:
        self.controls = Controls()
        self.operations = Operations()


class Participants:
    def __init__(self) -> None:
        self.requests: list[dict[str, str | None]] = []

    async def spawn(
        self,
        harness: str,
        prompt: str | None,
        approval: str,
        *,
        cwd: str,
        idempotency_key: str,
    ) -> object:
        self.requests.append(
            {
                "harness": harness,
                "prompt": prompt,
                "approval": approval,
                "cwd": cwd,
                "idempotency_key": idempotency_key,
            }
        )
        return await _accepted_spawn()


async def test_settled_history_is_bounded_without_losing_unreconciled_or_uncertain_actions():
    class FinishedParticipants:
        async def spawn(self, harness, prompt, approval, *, cwd, idempotency_key):
            return SimpleNamespace(
                value=AcceptedOperation(operation_id=idempotency_key, state="succeeded")
            )

    client = Client()
    client.participants = FinishedParticipants()
    client.controls.uncertain = True
    controller = OperationController(client, history_limit=2)
    uncertain = await controller.send("p", "retry me")
    controller.acknowledge(uncertain)
    unreconciled = await controller.spawn("codex", "not rendered", "manual", cwd="/tmp")
    for index in range(10):
        record = await controller.spawn("codex", str(index), "manual", cwd="/tmp")
        controller.acknowledge(record)
    assert len(controller.records) == 4
    assert uncertain in controller.records and unreconciled in controller.records
    assert set(controller._requests) == {("send", "p")}
    assert not controller._spawn_targets
    client.controls.uncertain = False
    assert await controller.retry("send", "p") is uncertain
    assert client.controls.keys == [uncertain.idempotency_key] * 2
    controller.acknowledge(unreconciled)
    assert len(controller.records) == 3
    await controller.close()


@pytest.mark.asyncio
async def test_pending_clicks_coalesce_on_stable_participant_id_and_retain_the_key() -> None:
    client = Client()
    controller = OperationController(cast(FrontendClient, client))

    first = await controller.send("participant-a", "hello")
    second = await controller.send("participant-a", "hello")

    assert first is second
    assert first.state is ActionState.PENDING
    assert first.operation_id == "operation-a"
    assert len(client.controls.keys) == 1
    await controller.close()


@pytest.mark.asyncio
async def test_uncertain_action_is_not_replayed_until_an_explicit_retry_uses_the_same_key() -> None:
    client = Client()
    client.controls.uncertain = True
    controller = OperationController(cast(FrontendClient, client))

    uncertain = await controller.send("participant-a", "hello")
    duplicate = await controller.send("participant-a", "hello")
    assert uncertain.state is ActionState.UNCERTAIN
    client.controls.uncertain = False
    retried = await controller.retry("send", "participant-a")

    assert uncertain is duplicate
    assert uncertain.state is ActionState.PENDING
    assert retried is uncertain
    assert client.controls.keys == [uncertain.idempotency_key, uncertain.idempotency_key]
    await controller.close()


@pytest.mark.asyncio
async def test_owned_action_client_is_retained_for_uncertainty_then_closed_on_success() -> None:
    class LifecycleControls(Controls):
        async def send(self, participant_id: str, prompt: str, *, idempotency_key: str) -> object:
            self.keys.append(idempotency_key)
            if self.uncertain:
                raise RequestUncertain("frontend.controls.send", len(self.keys))
            return SimpleNamespace(
                value=AcceptedOperation(
                    operation_id="operation-a",
                    state="succeeded",
                    participant_id=participant_id,
                )
            )

    class LifecycleClient(Client):
        def __init__(self) -> None:
            super().__init__()
            self.controls = LifecycleControls()
            self.controls.uncertain = True
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    action_client = LifecycleClient()
    controller = OperationController(
        cast(FrontendClient, Client()),
        client_factory=lambda: cast(FrontendClient, action_client),
    )

    record = await controller.send("participant-a", "hello")
    assert record.state is ActionState.UNCERTAIN
    assert action_client.close_calls == 0

    action_client.controls.uncertain = False
    assert await controller.retry("send", "participant-a") is record
    assert record.state is ActionState.SUCCEEDED
    assert action_client.controls.keys == [record.idempotency_key, record.idempotency_key]
    assert action_client.close_calls == 1
    assert controller._client_locks == {}

    await controller.close()
    assert action_client.close_calls == 1


@pytest.mark.asyncio
async def test_close_cancels_only_the_local_operation_wait() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class WaitingOperations:
        async def wait(self, operation_id: str, *, wait_seconds: int) -> object:
            assert (operation_id, wait_seconds) == ("operation-a", 30)
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            raise AssertionError("local wait unexpectedly completed")

    client = Client()
    client.operations = WaitingOperations()
    controller = OperationController(cast(FrontendClient, client))

    record = await controller.send("participant-a", "hello")
    await asyncio.wait_for(started.wait(), timeout=1)
    await controller.close()

    assert cancelled.is_set()
    assert record.operation_id == "operation-a"
    assert record.state is ActionState.PENDING


@pytest.mark.asyncio
async def test_refresh_detaches_an_active_wait_before_reobserving_the_operation() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class WaitingOperations:
        def __init__(self) -> None:
            self.gets = 0

        async def wait(self, operation_id: str, *, wait_seconds: int) -> object:
            assert (operation_id, wait_seconds) == ("operation-a", 30)
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            raise AssertionError("detached wait unexpectedly completed")

        async def get(self, operation_id: str) -> object:
            assert operation_id == "operation-a"
            self.gets += 1
            assert cancelled.is_set()
            return SimpleNamespace(value=SimpleNamespace(state="succeeded", error=None))

    client = Client()
    operations = WaitingOperations()
    client.operations = operations
    controller = OperationController(cast(FrontendClient, client))

    record = await controller.send("participant-a", "hello")
    await asyncio.wait_for(started.wait(), timeout=1)

    await asyncio.wait_for(controller.refresh_pending(), timeout=1)

    assert operations.gets == 1
    assert cancelled.is_set()
    assert record.state is ActionState.SUCCEEDED
    await controller.close()


@pytest.mark.asyncio
async def test_spawn_forwards_cwd_omits_bare_prompt_and_coalesces_per_directory() -> None:
    client = Client()
    participants = Participants()
    client.participants = participants
    controller = OperationController(cast(FrontendClient, client))

    first = await controller.spawn("codex", "", "yolo", cwd="/workspace/one")
    duplicate = await controller.spawn("codex", "", "yolo", cwd="/workspace/one")
    other_directory = await controller.spawn("codex", "", "yolo", cwd="/workspace/two")

    assert first is duplicate
    assert first is not other_directory
    assert [request["cwd"] for request in participants.requests] == [
        "/workspace/one",
        "/workspace/two",
    ]
    assert [request["prompt"] for request in participants.requests] == [None, None]
    await controller.close()


@pytest.mark.asyncio
async def test_wait_timeout_is_observed_again_without_replaying_the_mutation() -> None:
    class EventuallyDoneOperations:
        def __init__(self) -> None:
            self.waits = 0

        async def wait(self, operation_id: str, *, wait_seconds: int) -> object:
            assert (operation_id, wait_seconds) == ("operation-a", 30)
            self.waits += 1
            if self.waits == 1:
                return SimpleNamespace(
                    value=SimpleNamespace(
                        timed_out=True,
                        operation=SimpleNamespace(state="running", error=None),
                    )
                )
            return SimpleNamespace(
                value=SimpleNamespace(
                    timed_out=False,
                    operation=SimpleNamespace(state="succeeded", error=None),
                )
            )

    client = Client()
    operations = EventuallyDoneOperations()
    client.operations = operations
    controller = OperationController(cast(FrontendClient, client))

    record = await controller.send("participant-a", "hello")
    for _ in range(10):
        if record.state is ActionState.SUCCEEDED:
            break
        await asyncio.sleep(0)

    assert record.state is ActionState.SUCCEEDED
    assert operations.waits == 2
    assert len(client.controls.keys) == 1
    await controller.close()


@pytest.mark.asyncio
async def test_reconnect_restarts_one_waiter_for_an_uncertain_accepted_operation() -> None:
    lost_connection = asyncio.Event()

    class RecoveringOperations:
        def __init__(self) -> None:
            self.waits = 0
            self.gets = 0

        async def wait(self, operation_id: str, *, wait_seconds: int) -> object:
            assert (operation_id, wait_seconds) == ("operation-a", 30)
            self.waits += 1
            if self.waits == 1:
                lost_connection.set()
                raise RequestUncertain("frontend.operations.await", 2)
            return SimpleNamespace(
                value=SimpleNamespace(
                    timed_out=False,
                    operation=SimpleNamespace(state="succeeded", error=None),
                )
            )

        async def get(self, operation_id: str) -> object:
            assert operation_id == "operation-a"
            self.gets += 1
            return SimpleNamespace(value=SimpleNamespace(state="running", error=None))

    client = Client()
    operations = RecoveringOperations()
    client.operations = operations
    controller = OperationController(cast(FrontendClient, client))

    record = await controller.send("participant-a", "hello")
    await asyncio.wait_for(lost_connection.wait(), timeout=1)
    for _ in range(10):
        if record.state is ActionState.UNCERTAIN:
            break
        await asyncio.sleep(0)
    assert record.state is ActionState.UNCERTAIN

    await controller.refresh_pending()
    for _ in range(10):
        if record.state is ActionState.SUCCEEDED:
            break
        await asyncio.sleep(0)

    assert record.state is ActionState.SUCCEEDED
    assert operations.gets == 1
    assert operations.waits == 2
    assert len(client.controls.keys) == 1
    await controller.close()


@pytest.mark.asyncio
async def test_unknown_observed_operation_state_stops_waiting_as_uncertain() -> None:
    class UnknownOperations:
        def __init__(self) -> None:
            self.waits = 0

        async def wait(self, operation_id: str, *, wait_seconds: int) -> object:
            assert (operation_id, wait_seconds) == ("operation-a", 30)
            self.waits += 1
            return SimpleNamespace(
                value=SimpleNamespace(
                    timed_out=False,
                    operation=SimpleNamespace(state="future-state", error=None),
                )
            )

    client = Client()
    operations = UnknownOperations()
    client.operations = operations
    controller = OperationController(cast(FrontendClient, client))

    record = await controller.send("participant-a", "hello")
    for _ in range(10):
        if record.state is ActionState.UNCERTAIN:
            break
        await asyncio.sleep(0)

    assert record.state is ActionState.UNCERTAIN
    assert "future-state" in (record.detail or "")
    assert operations.waits == 1
    await controller.close()


@pytest.mark.asyncio
async def test_unverifiable_mutation_response_remains_retryable_as_uncertain() -> None:
    class InvalidControls(Controls):
        async def send(self, participant_id: str, prompt: str, *, idempotency_key: str) -> object:
            del participant_id, prompt
            self.keys.append(idempotency_key)
            raise ResponseValidationError("invalid accepted-operation payload")

    client = Client()
    client.controls = InvalidControls()
    controller = OperationController(cast(FrontendClient, client))

    record = await controller.send("participant-a", "hello")

    assert record.state is ActionState.UNCERTAIN
    assert "cannot verify action response" in (record.detail or "")
    assert client.controls.keys == [record.idempotency_key]
    await controller.close()


@pytest.mark.parametrize(
    ("action", "message"),
    [
        ("send", "delivery unknown"),
        ("steer", "delivery unknown"),
        ("queue_followup", "delivery unknown"),
        ("interrupt", "delivery unknown"),
        ("settings_update", "outcome unknown"),
    ],
)
def test_success_without_receipt_evidence_is_rendered_as_a_warning(
    action: str,
    message: str,
) -> None:
    record = ActionRecord(
        action,
        "participant-a",
        "key-a",
        state=ActionState.SUCCEEDED,
        result={},
    )

    rendered, severity = describe_action(record)

    assert message in rendered
    assert severity == "warning"


@pytest.mark.parametrize("action", ["send", "steer", "queue_followup", "interrupt"])
@pytest.mark.parametrize(
    ("delivery", "message", "severity"),
    [
        ("unknown", "do not retry blindly", "warning"),
        ("pending", "delivery not confirmed yet", "warning"),
        ("rejected", "rejected", "error"),
        ("refused", "rejected", "error"),
    ],
)
def test_delivery_receipt_semantics_override_completed_operation_state(
    action: str,
    delivery: str,
    message: str,
    severity: str,
) -> None:
    record = ActionRecord(
        action,
        "participant-a",
        "key-a",
        state=ActionState.SUCCEEDED,
        job_handle="job-a" if action == "queue_followup" else None,
        result={"delivery": delivery, "reason": "receipt reason"},
    )

    rendered, actual_severity = describe_action(record)

    assert message in rendered
    assert "receipt reason" in rendered
    assert actual_severity == severity


@pytest.mark.parametrize("reason", ["already_idle", "already_not_working"])
def test_interrupt_false_is_informational_only_for_idle_reasons(reason: str) -> None:
    record = ActionRecord(
        "interrupt",
        "participant-a",
        "key-a",
        state=ActionState.SUCCEEDED,
        result={"interrupted": False, "reason": reason},
    )

    rendered, severity = describe_action(record)

    assert rendered == f"nothing to interrupt — {reason}"
    assert severity == "information"


@pytest.mark.parametrize("reason", ["delivery_unknown", "human_present", None])
def test_interrupt_false_with_any_other_reason_is_a_warning(reason: str | None) -> None:
    result: dict[str, object] = {"interrupted": False}
    if reason is not None:
        result["reason"] = reason
    record = ActionRecord(
        "interrupt",
        "participant-a",
        "key-a",
        state=ActionState.SUCCEEDED,
        result=result,
    )

    rendered, severity = describe_action(record)

    assert "interrupt not performed" in rendered
    assert severity == "warning"


def test_interrupt_unknown_delivery_warns_against_a_blind_retry() -> None:
    record = ActionRecord(
        "interrupt",
        "participant-a",
        "key-a",
        state=ActionState.SUCCEEDED,
        result={"delivery": "unknown", "interrupted": False, "reason": "delivery_unknown"},
    )

    rendered, severity = describe_action(record)

    assert "do not retry blindly" in rendered
    assert severity == "warning"


@pytest.mark.parametrize(
    ("result", "message", "severity"),
    [
        ({"applied": True}, "settings updated", "information"),
        ({"applied": False, "error_code": "busy"}, "refused", "error"),
        ({"delivery": "accepted"}, "settings updated", "information"),
        ({"delivery": "unknown"}, "do not retry blindly", "warning"),
        ({"delivery": "pending"}, "delivery not confirmed yet", "warning"),
        ({"delivery": "rejected"}, "rejected", "error"),
    ],
)
def test_settings_supports_legacy_and_durable_delivery_receipts(
    result: dict[str, object],
    message: str,
    severity: str,
) -> None:
    record = ActionRecord(
        "settings_update",
        "participant-a",
        "key-a",
        state=ActionState.SUCCEEDED,
        result=result,
    )

    rendered, actual_severity = describe_action(record)

    assert message in rendered
    assert actual_severity == severity


def test_queue_success_requires_a_handle_or_acknowledged_delivery() -> None:
    handle = ActionRecord(
        "queue_followup",
        "participant-a",
        "key-a",
        state=ActionState.SUCCEEDED,
        job_handle="job-a",
        result={},
    )
    acknowledged = ActionRecord(
        "queue_followup",
        "participant-a",
        "key-b",
        state=ActionState.SUCCEEDED,
        result={"delivery": "accepted"},
    )
    phase_acknowledged = ActionRecord(
        "queue_followup",
        "participant-a",
        "key-c",
        state=ActionState.SUCCEEDED,
        phase="delivery_acknowledged",
        result={},
    )

    assert describe_action(handle) == ("followup queued as job-a", "information")
    assert describe_action(acknowledged) == ("queue followup accepted", "information")
    assert describe_action(phase_acknowledged) == (
        "queue followup accepted",
        "information",
    )


def test_controls_report_preserves_rc9_runtime_context_from_public_extras() -> None:
    controls = PublicControls.from_wire(
        {
            "actions": {
                "send": {
                    "supported": True,
                    "route_available": True,
                    "admissible": True,
                },
                "steer": {
                    "supported": True,
                    "route_available": True,
                    "admissible": False,
                    "reason": "busy",
                    "detail": "another turn is active",
                },
            },
            "revision": 7,
            "wiring": "native",
            "health": {
                "connection": "connected",
                "diagnostics": ["runtime recovered"],
            },
            "settings": {"model": "gpt-5", "reasoning_effort": "high"},
            "active_turn": {"native_turn_id": "turn-a", "job_handle": "job-active"},
            "queued": [{"handle": "job-next"}, "job-later"],
        }
    )

    assert format_controls_report(controls).splitlines() == [
        "wiring: native",
        "health: connection=connected",
        "diagnostics: runtime recovered",
        "send: available",
        "steer: unavailable — busy (another turn is active)",
        "settings: model=gpt-5, reasoning_effort=high",
        "active turn: turn-a",
        "queued followups: 2 (job-next, job-later)",
    ]


async def _accepted_spawn() -> object:
    return SimpleNamespace(
        value=AcceptedOperation(
            operation_id="operation-a",
            state="accepted",
            participant_id="participant-a",
        )
    )
