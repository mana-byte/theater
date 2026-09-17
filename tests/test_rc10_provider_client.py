"""Focused fixture-daemon checks for the RC10 terminal-provider callback client."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from theater.frontend.provider import (
    CallbackRequest,
    CallbackResponse,
    ProviderClient,
    ProviderProtocolError,
)

Handler = Callable[[CallbackRequest], Awaitable[Mapping[str, object] | CallbackResponse]]


@dataclass
class _Session:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    async def send(self, value: Mapping[str, object]) -> None:
        self.writer.write(json.dumps(dict(value), separators=(",", ":")).encode("utf-8") + b"\n")
        await self.writer.drain()

    async def send_raw(self, value: bytes) -> None:
        self.writer.write(value)
        await self.writer.drain()

    async def read(self) -> dict[str, object]:
        frame = await asyncio.wait_for(self.reader.readline(), timeout=1)
        assert frame.endswith(b"\n")
        value = json.loads(frame)
        assert isinstance(value, dict)
        return value

    async def close(self) -> None:
        self.writer.close()
        with contextlib.suppress(OSError, ConnectionError):
            await self.writer.wait_closed()


class _FixtureDaemon:
    def __init__(self, socket_path: Path, sessions: asyncio.Queue[_Session]) -> None:
        self.socket_path = socket_path
        self._sessions = sessions

    async def next_session(self) -> _Session:
        return await asyncio.wait_for(self._sessions.get(), timeout=1)


@asynccontextmanager
async def _fixture_daemon(  # noqa: PLR0915
    *, limits: Mapping[str, object] | None = None
) -> AsyncIterator[_FixtureDaemon]:
    root = Path(tempfile.mkdtemp(prefix="r10provider-", dir="/tmp"))
    socket_path = root / "frontend.sock"
    sessions: asyncio.Queue[_Session] = asyncio.Queue()
    stop = asyncio.Event()
    tasks: set[asyncio.Task[None]] = set()
    writers: set[asyncio.StreamWriter] = set()
    failures: list[BaseException] = []
    handshake_limits: dict[str, object] = {
        "max_frame_bytes": 67_108_864,
        "provider_pending_callbacks": 32,
        "provider_callback_timeout_seconds": 30,
        "provider_lease_seconds": 30,
        "provider_mutations_per_terminal": 1,
    }
    if limits is not None:
        handshake_limits.update(limits)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        writers.add(writer)
        try:
            frame = await asyncio.wait_for(reader.readline(), timeout=1)
            request = json.loads(frame)
            assert request["id"] == 1
            assert request["method"] == "frontend.handshake"
            params = request["params"]
            assert params == {
                "api": {"major": 1, "minor": 0},
                "client_id": "provider-fixture",
                "role": "provider",
                "channel": "callback",
                "required_capabilities": ["terminal-provider.v1"],
                "provider_id": "provider-a",
                "provider_credential": "credential-a",
            }
            response = {
                "id": 1,
                "ok": True,
                "result": {
                    "api": {"major": 1, "minor": 0},
                    "daemon_instance_id": "fixture-daemon",
                    "package_version": "1.0.0rc10",
                    "capabilities": ["terminal-provider.v1"],
                    "limits": handshake_limits,
                    "provider_generation": 7,
                    "future_handshake_field": {"kept": True},
                },
            }
            writer.write(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
            await writer.drain()
            await sessions.put(_Session(reader, writer))
            await stop.wait()
        except BaseException as exc:
            failures.append(exc)
        finally:
            writers.discard(writer)
            tasks.discard(task)
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()

    server = await asyncio.start_unix_server(serve, path=str(socket_path))
    try:
        daemon = _FixtureDaemon(socket_path, sessions)
        yield daemon
    finally:
        stop.set()
        server.close()
        await server.wait_closed()
        for writer in tuple(writers):
            writer.close()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=1)
        socket_path.unlink(missing_ok=True)
        shutil.rmtree(root)
        if failures:
            raise failures[0]


def _client(socket_path: Path, handlers: Mapping[str, Handler]) -> ProviderClient:
    return ProviderClient(
        socket_path,
        client_id="provider-fixture",
        provider_id="provider-a",
        provider_credential="credential-a",
        handlers=handlers,
    )


async def _connect(client: ProviderClient, daemon: _FixtureDaemon) -> _Session:
    connect = asyncio.create_task(client.connect())
    session = await daemon.next_session()
    result = await connect
    assert result.provider_generation == 7
    return session


async def _eventually(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0.001)


def _mapping_value(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    nested = value[key]
    assert isinstance(nested, Mapping)
    return nested


def _callback_id(value: Mapping[str, object]) -> str:
    callback_id = value["id"]
    assert isinstance(callback_id, str)
    return callback_id


def _error_code(value: Mapping[str, object]) -> str:
    code = _mapping_value(value, "error")["code"]
    assert isinstance(code, str)
    return code


def _inventory(callback_id: str, *, generation: int = 7) -> dict[str, object]:
    return {
        "type": "request",
        "id": callback_id,
        "method": "terminal.inventory",
        "params": {"provider_generation": generation},
    }


def _inspect(callback_id: str, *, generation: int = 7) -> dict[str, object]:
    return {
        "type": "request",
        "id": callback_id,
        "method": "terminal.inspect",
        "params": {
            "provider_generation": generation,
            "terminal_id": "terminal-a",
            "terminal_incarnation": "incarnation-a",
        },
    }


def _deliver(
    callback_id: str,
    *,
    terminal_id: str = "terminal-a",
    terminal_incarnation: str = "incarnation-a",
    generation: int = 7,
) -> dict[str, object]:
    return {
        "type": "request",
        "id": callback_id,
        "method": "terminal.deliver",
        "params": {
            "operation_id": f"operation-{callback_id}",
            "provider_generation": generation,
            "participant_id": "participant-a",
            "terminal_id": terminal_id,
            "terminal_incarnation": terminal_incarnation,
            "expected_occupant": "occupant-a",
            "action": {"kind": "submit_text", "text": "Review this."},
            "require_absent": True,
        },
    }


def _create(
    callback_id: str, *, launch_id: str = "launch-a", generation: int = 7
) -> dict[str, object]:
    return {
        "type": "request",
        "id": callback_id,
        "method": "terminal.create",
        "params": {
            "operation_id": f"operation-{callback_id}",
            "provider_generation": generation,
            "participant_id": "participant-a",
            "launch_id": launch_id,
            "launch": {
                "executable": "codex",
                "argv": ["codex", "--resume"],
                "cwd": "/tmp",
                "environment": {},
            },
        },
    }


def _result(request: CallbackRequest) -> dict[str, object]:
    params = request.params
    if request.method == "terminal.inventory":
        return {
            "provider_generation": params["provider_generation"],
            "report_revision": 1,
            "complete": True,
            "terminals": [],
        }
    if request.method == "terminal.create":
        return {
            "operation_id": params["operation_id"],
            "provider_generation": params["provider_generation"],
            "outcome": "accepted",
        }
    return {
        "operation_id": params["operation_id"],
        "provider_generation": params["provider_generation"],
        "terminal_id": params["terminal_id"],
        "terminal_incarnation": params["terminal_incarnation"],
        "delivery": "accepted",
    }


@pytest.mark.asyncio
async def test_provider_connects_with_callback_handshake_and_preserves_future_values() -> None:
    async def handler(request: CallbackRequest) -> Mapping[str, object] | CallbackResponse:
        if request.method == "terminal.inventory":
            return {**_result(request), "future_result_field": {"kept": True}}
        return CallbackResponse(
            error={
                "code": "future_provider_refusal",
                "message": "A newer provider rejected inspection.",
                "future_error_field": ["kept"],
            }
        )

    async with _fixture_daemon() as daemon:
        client = _client(
            daemon.socket_path, {"terminal.inventory": handler, "terminal.inspect": handler}
        )
        session = await _connect(client, daemon)
        assert client.handshake_result is not None
        assert client.handshake_result.extra["future_handshake_field"] == {"kept": True}

        await session.send(_inventory("inventory-a"))
        inventory = await session.read()
        await session.send(_inspect("inspect-a"))
        refusal = await session.read()
        await client.close()

    assert _mapping_value(inventory, "result")["future_result_field"] == {"kept": True}
    assert _error_code(refusal) == "future_provider_refusal"
    assert _mapping_value(refusal, "error")["future_error_field"] == ["kept"]


@pytest.mark.asyncio
async def test_independent_terminal_callbacks_run_concurrently_without_blocking_reads() -> None:
    started: set[str] = set()
    both_started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: CallbackRequest) -> Mapping[str, object]:
        if request.method == "terminal.inventory":
            return _result(request)
        terminal_id = request.params["terminal_id"]
        assert isinstance(terminal_id, str)
        started.add(terminal_id)
        if started == {"terminal-a", "terminal-b"}:
            both_started.set()
        await release.wait()
        return _result(request)

    async with _fixture_daemon() as daemon:
        client = _client(
            daemon.socket_path, {"terminal.deliver": handler, "terminal.inventory": handler}
        )
        session = await _connect(client, daemon)
        await session.send(_deliver("deliver-a", terminal_id="terminal-a"))
        await session.send(_deliver("deliver-b", terminal_id="terminal-b"))
        await asyncio.wait_for(both_started.wait(), timeout=1)
        await session.send(_inventory("inventory-while-waiting"))
        inventory = await session.read()
        assert _callback_id(inventory) == "inventory-while-waiting"
        release.set()
        responses = {_callback_id(await session.read()) for _ in range(2)}
        await client.close()

    assert responses == {"deliver-a", "deliver-b"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first,second", [(_deliver("first"), _deliver("second")), (_create("first"), _create("second"))]
)
async def test_same_identity_mutations_serialize_before_their_handler_starts(
    first: dict[str, object], second: dict[str, object]
) -> None:
    active = 0
    maximum_active = 0
    order: list[str] = []
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()

    async def handler(request: CallbackRequest) -> Mapping[str, object]:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        order.append(request.callback_id)
        if request.callback_id == "first":
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        active -= 1
        return _result(request)

    async with _fixture_daemon() as daemon:
        client = _client(
            daemon.socket_path,
            {"terminal.deliver": handler, "terminal.create": handler},
        )
        session = await _connect(client, daemon)
        await session.send(first)
        await session.send(second)
        await asyncio.wait_for(first_started.wait(), timeout=1)
        await _eventually(lambda: client.pending_callbacks == 2)
        assert not second_started.is_set()
        release_first.set()
        await asyncio.wait_for(second_started.wait(), timeout=1)
        response_ids = {_callback_id(await session.read()) for _ in range(2)}
        await client.close()

    assert order == ["first", "second"]
    assert maximum_active == 1
    assert response_ids == {"first", "second"}


@pytest.mark.asyncio
async def test_saturation_refuses_before_dispatch() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def handler(request: CallbackRequest) -> Mapping[str, object]:
        calls.append(request.callback_id)
        started.set()
        await release.wait()
        return _result(request)

    async with _fixture_daemon(limits={"provider_pending_callbacks": 1}) as daemon:
        client = _client(daemon.socket_path, {"terminal.inventory": handler})
        session = await _connect(client, daemon)
        await session.send(_inventory("slow"))
        await asyncio.wait_for(started.wait(), timeout=1)
        await session.send(_inventory("slow"))
        await asyncio.sleep(0)
        assert calls == ["slow"]
        await session.send(_inventory("saturated"))
        refusal = await session.read()
        assert _callback_id(refusal) == "saturated"
        assert _error_code(refusal) == "provider_busy"
        assert calls == ["slow"]
        release.set()
        completed = await session.read()
        await client.close()

    assert _callback_id(completed) == "slow"


@pytest.mark.asyncio
async def test_generation_loss_drops_a_same_terminal_callback_before_dispatch() -> None:
    started: list[str] = []
    first_started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: CallbackRequest) -> Mapping[str, object]:
        started.append(request.callback_id)
        if request.callback_id == "first":
            first_started.set()
            await release.wait()
        return _result(request)

    async with _fixture_daemon() as daemon:
        client = _client(daemon.socket_path, {"terminal.deliver": handler})
        session = await _connect(client, daemon)
        await session.send(_deliver("first"))
        await session.send(_deliver("stale"))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        await _eventually(lambda: client.pending_callbacks == 2)
        client.invalidate_generation(generation=7)
        release.set()
        responses = {
            _callback_id(response): response
            for response in (await session.read(), await session.read())
        }
        await client.close()

    assert started == ["first"]
    assert _mapping_value(responses["first"], "result")["delivery"] == "unknown"
    assert _error_code(responses["stale"]) == "stale_generation"


@pytest.mark.asyncio
async def test_timed_out_mutation_duplicate_replays_unknown_during_handler() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(request: CallbackRequest) -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return _result(request)

    async with _fixture_daemon() as daemon:
        client = ProviderClient(
            daemon.socket_path,
            client_id="provider-fixture",
            provider_id="provider-a",
            provider_credential="credential-a",
            handlers={"terminal.deliver": handler},
            callback_timeout=0.01,
        )
        session = await _connect(client, daemon)
        request = _deliver("timed-out")
        await session.send(request)
        await asyncio.wait_for(started.wait(), timeout=1)
        first = await session.read()
        assert _mapping_value(first, "result")["delivery"] == "unknown"

        await session.send(request)
        replay = await asyncio.wait_for(session.read(), timeout=0.1)
        assert replay == first
        assert calls == 1

        release.set()
        await _eventually(lambda: client.pending_callbacks == 0)
        await client.close()


@pytest.mark.asyncio
async def test_started_mutation_survives_connection_loss_without_a_rollback_promise() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(request: CallbackRequest) -> Mapping[str, object]:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finished.set()
        return _result(request)

    async with _fixture_daemon() as daemon:
        client = _client(daemon.socket_path, {"terminal.deliver": handler})
        session = await _connect(client, daemon)
        await session.send(_deliver("loss"))
        await asyncio.wait_for(started.wait(), timeout=1)
        await session.close()
        await asyncio.wait_for(client.wait_closed(), timeout=1)
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=1)
        await client.close()

    assert not cancelled.is_set()


@pytest.mark.asyncio
async def test_cancelled_mutation_and_late_or_unknown_frames_are_safe() -> None:
    calls = 0

    async def handler(request: CallbackRequest) -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        if request.callback_id == "cancelled":
            raise asyncio.CancelledError
        return _result(request)

    async with _fixture_daemon() as daemon:
        client = _client(
            daemon.socket_path, {"terminal.deliver": handler, "terminal.inventory": handler}
        )
        session = await _connect(client, daemon)
        await session.send(_deliver("cancelled"))
        cancelled = await session.read()
        assert _mapping_value(cancelled, "result")["delivery"] == "unknown"

        await session.send(
            {"type": "request", "id": "unknown", "method": "terminal.future", "params": {}}
        )
        unknown = await session.read()
        assert _error_code(unknown) == "unknown_method"

        await session.send(_inventory("duplicate"))
        first = await session.read()
        await session.send(_inventory("duplicate"))
        duplicate = await session.read()
        assert first == duplicate
        assert calls == 2

        await session.send({"type": "response", "id": "late", "result": {}})
        await asyncio.wait_for(client.wait_closed(), timeout=1)
        assert isinstance(client.last_error, ProviderProtocolError)
        await client.close()


@pytest.mark.asyncio
async def test_negotiated_frame_limits_reject_oversized_inbound_and_outbound_frames() -> None:
    async def large_handler(request: CallbackRequest) -> Mapping[str, object]:
        return {**_result(request), "future_payload": "x" * 1_024}

    async with _fixture_daemon(limits={"max_frame_bytes": 512}) as daemon:
        client = _client(daemon.socket_path, {"terminal.inventory": large_handler})
        session = await _connect(client, daemon)
        await session.send(_inventory("outbound"))
        too_large = await session.read()
        assert _error_code(too_large) == "too_large"
        await client.close()

        inbound = _client(daemon.socket_path, {"terminal.inventory": large_handler})
        session = await _connect(inbound, daemon)
        await session.send_raw(
            b'{"type":"request","id":"oversized","payload":"' + b"x" * 600 + b'"}\n'
        )
        await asyncio.wait_for(inbound.wait_closed(), timeout=1)
        assert isinstance(inbound.last_error, ProviderProtocolError)
        await inbound.close()
