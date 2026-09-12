"""Duplex request failures must never cause automatic mutation replay."""

from __future__ import annotations

import asyncio
import json

import pytest

from theater.daemon.harness_runtime.errors import RuntimePayloadTooLarge
from theater.daemon.harness_runtime.frontend_requests import FrontendRequests
from theater.harness.contracts.runtime import (
    RuntimeConnectionClosed,
    RuntimeRequestError,
    RuntimeRequestTimeout,
)


@pytest.mark.asyncio
async def test_frontend_requests_correlate_out_of_order_and_remote_errors():
    sent = asyncio.Queue()

    async def send(payload):
        sent.put_nowait(json.loads(payload))

    requests = FrontendRequests(send)
    first = asyncio.create_task(requests.request("pi.snapshot", {}, timeout=1))
    second = asyncio.create_task(requests.request("pi.settings.update", {}, timeout=1))
    first_frame, second_frame = await sent.get(), await sent.get()
    assert first_frame["id"] != second_frame["id"]
    requests.receive(
        {
            "type": "response",
            "id": second_frame["id"],
            "error": {"code": "busy", "message": "not idle"},
        }
    )
    requests.receive({"type": "response", "id": first_frame["id"], "result": {"session": "a"}})
    assert await first == {"session": "a"}
    with pytest.raises(RuntimeRequestError) as error:
        await second
    assert error.value.code == "busy"
    requests.close()


@pytest.mark.asyncio
async def test_frontend_request_timeout_discards_late_reply_without_replay():
    sent = []

    async def send(payload):
        sent.append(json.loads(payload))

    requests = FrontendRequests(send)
    with pytest.raises(RuntimeRequestTimeout):
        await requests.request("pi.settings.update", {"operation_id": "once"}, timeout=0.01)
    assert len(sent) == 1
    assert not requests.receive({"type": "response", "id": sent[0]["id"], "result": {}})
    assert requests._pending == {}
    requests.close()


@pytest.mark.asyncio
async def test_frontend_close_fails_requests_on_old_peer():
    sent = asyncio.Queue()

    async def send(payload):
        sent.put_nowait(json.loads(payload))

    requests = FrontendRequests(send)
    task = asyncio.create_task(requests.request("pi.settings.update", {}, timeout=1))
    await sent.get()
    requests.close()
    with pytest.raises(RuntimeConnectionClosed):
        await task
    with pytest.raises(RuntimeConnectionClosed):
        await requests.request("pi.snapshot", {}, timeout=1)
    assert requests._pending == {}


@pytest.mark.asyncio
async def test_frontend_oversized_request_is_rejected_before_transmission():
    async def send(_payload):
        pytest.fail("oversized request must not reach the writer")

    requests = FrontendRequests(send)
    with pytest.raises(RuntimePayloadTooLarge):
        await requests.request("pi.settings.update", {"value": "x" * 65536}, timeout=1)
    requests.close()
