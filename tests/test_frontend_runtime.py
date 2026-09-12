"""Authenticated passive frontend listener coverage."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from theater.daemon.harness_runtime.constants import FRONTEND_QUEUE_MAX
from theater.daemon.harness_runtime.frontend import (
    FrontendProtocolError,
    FrontendRuntimeHost,
    UnixFrontendConnection,
)
from theater.daemon.runtime.wiring import frontend_endpoint
from theater.harness.contracts.runtime import RuntimeFrontendConnection, RuntimeNotification


def _short_runtime_dir(monkeypatch, tmp_path) -> Path:
    directory = Path("/tmp") / tmp_path.name
    directory.mkdir(exist_ok=True)
    monkeypatch.setattr("theater.daemon.runtime.wiring.backend_artifacts_dir", lambda _: directory)
    monkeypatch.setattr(
        "theater.daemon.harness_runtime.frontend.backend_artifacts_dir", lambda _: directory
    )
    return directory


async def _connect(endpoint: str, token: str):
    path = endpoint.removeprefix("unix://")
    reader, writer = await asyncio.open_unix_connection(path)
    hello = {"type": "hello", "protocol": "theater-frontend-v1", "token": token}
    writer.write(json.dumps(hello).encode())
    writer.write(b"\n")
    await writer.drain()
    return reader, writer


async def _event(writer, payload: dict) -> None:
    writer.write(json.dumps({"type": "event", "event": payload}).encode())
    writer.write(b"\n")
    await writer.drain()


async def _close(writer) -> None:
    writer.close()
    await writer.wait_closed()


async def _wait_for_count(items: list[object], count: int) -> None:
    for _ in range(20):
        if len(items) >= count:
            return
        await asyncio.sleep(0)
    assert len(items) >= count


async def test_listener_authenticates_and_forwards_bounded_notifications(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    _short_runtime_dir(monkeypatch, tmp_path)
    host = FrontendRuntimeHost()
    endpoint = frontend_endpoint("open-1")
    connected: list[RuntimeFrontendConnection] = []
    disconnected: list[RuntimeFrontendConnection] = []

    async def on_connect(connection: RuntimeFrontendConnection) -> None:
        connected.append(connection)

    async def on_disconnect(connection: RuntimeFrontendConnection) -> None:
        disconnected.append(connection)

    await host.start(
        participant_id="open-1",
        generation=1,
        endpoint=endpoint,
        token="secret",
        on_connect=on_connect,
        on_disconnect=on_disconnect,
    )
    _reader, rejected = await _connect(endpoint, "wrong")
    await asyncio.sleep(0)
    assert connected == []
    await _close(rejected)

    _reader, writer = await _connect(endpoint, "secret")
    await _wait_for_count(connected, 1)
    assert len(connected) == 1
    await _event(writer, {"id": "event-1", "type": "message.updated"})
    notification = await anext(connected[0].notifications())
    assert notification.method == "event"
    assert notification.params["event"]["id"] == "event-1"
    await _close(writer)
    await _wait_for_count(disconnected, 1)
    assert disconnected == connected
    await host.aclose()


async def test_listener_replacement_disconnects_only_the_replaced_connection(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    _short_runtime_dir(monkeypatch, tmp_path)
    host = FrontendRuntimeHost()
    endpoint = frontend_endpoint("open-1")
    connected: list[RuntimeFrontendConnection] = []
    disconnected: list[RuntimeFrontendConnection] = []

    async def on_connect(connection: RuntimeFrontendConnection) -> None:
        connected.append(connection)

    async def on_disconnect(connection: RuntimeFrontendConnection) -> None:
        disconnected.append(connection)

    await host.start(
        participant_id="open-1",
        generation=1,
        endpoint=endpoint,
        token="secret",
        on_connect=on_connect,
        on_disconnect=on_disconnect,
    )
    _reader, first = await _connect(endpoint, "secret")
    await _wait_for_count(connected, 1)
    _reader, second = await _connect(endpoint, "secret")
    await _wait_for_count(connected, 2)
    assert len(connected) == 2
    assert disconnected == [connected[0]]
    assert not connected[1].closed
    await _close(first)
    await _close(second)
    await _wait_for_count(disconnected, 2)
    assert disconnected == connected
    await host.aclose()


async def test_listener_serializes_replacing_connection_activation(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    _short_runtime_dir(monkeypatch, tmp_path)
    host = FrontendRuntimeHost()
    endpoint = frontend_endpoint("open-1")
    connected: list[RuntimeFrontendConnection] = []
    disconnected: list[RuntimeFrontendConnection] = []
    first_ready = asyncio.Event()
    release_first = asyncio.Event()

    async def on_connect(connection: RuntimeFrontendConnection) -> None:
        connected.append(connection)
        if len(connected) == 1:
            first_ready.set()
            await release_first.wait()

    async def on_disconnect(connection: RuntimeFrontendConnection) -> None:
        disconnected.append(connection)

    await host.start(
        participant_id="open-1",
        generation=1,
        endpoint=endpoint,
        token="secret",
        on_connect=on_connect,
        on_disconnect=on_disconnect,
    )
    _reader, first = await _connect(endpoint, "secret")
    await first_ready.wait()
    _reader, second = await _connect(endpoint, "secret")
    await asyncio.sleep(0)
    assert len(connected) == 1

    release_first.set()
    await _wait_for_count(connected, 2)
    assert disconnected == [connected[0]]
    await _close(first)
    await _close(second)
    await host.aclose()


async def test_listener_refuses_an_endpoint_outside_participant_runtime(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    host = FrontendRuntimeHost()

    async def callback(connection: RuntimeFrontendConnection) -> None:
        del connection

    with pytest.raises(FrontendProtocolError, match="outside"):
        await host.start(
            participant_id="open-1",
            generation=1,
            endpoint=f"unix://{tmp_path / 'outside.sock'}",
            token="secret",
            on_connect=callback,
            on_disconnect=callback,
        )


async def test_closed_connection_drains_a_full_queue_then_terminates() -> None:
    class Writer:
        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    connection = UnixFrontendConnection(Writer())  # type: ignore[arg-type]
    for index in range(FRONTEND_QUEUE_MAX):
        connection.publish(RuntimeNotification(method="snapshot", params={"index": index}))

    await connection.aclose()

    async def drain() -> list[RuntimeNotification]:
        return [notification async for notification in connection.notifications()]

    drained = await asyncio.wait_for(drain(), timeout=0.5)
    assert len(drained) == FRONTEND_QUEUE_MAX
