from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest
from regie.controllers.actions import ActionState, OperationController

from theater.frontend import AcceptedOperation, FrontendClient, RequestUncertain


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
async def test_spawn_clicks_coalesce_and_reconnect_reobserves_the_accepted_handle() -> None:
    client = Client()
    client.participants = SimpleNamespace(
        spawn=lambda *_args, **_kwargs: _accepted_spawn(),
    )
    controller = OperationController(cast(FrontendClient, client))

    first = await controller.spawn("codex", "inspect the change", "yolo")
    second = await controller.spawn("codex", "inspect the change", "yolo")
    await controller.refresh_pending()

    assert first is second
    assert first.state is ActionState.SUCCEEDED
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


async def _accepted_spawn() -> object:
    return SimpleNamespace(
        value=AcceptedOperation(
            operation_id="operation-a",
            state="accepted",
            participant_id="participant-a",
        )
    )
