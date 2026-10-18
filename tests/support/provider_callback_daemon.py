"""Scripted provider callback handshake server and frame builders for provider tests."""

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

from theater.frontend.provider import (
    CallbackRequest,
    CallbackResponse,
    ProviderClient,
)

Handler = Callable[[CallbackRequest], Awaitable[Mapping[str, object] | CallbackResponse]]


@dataclass
class CallbackSession:
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


class FixtureDaemon:
    def __init__(self, socket_path: Path, sessions: asyncio.Queue[CallbackSession]) -> None:
        self.socket_path = socket_path
        self._sessions = sessions

    async def next_session(self) -> CallbackSession:
        return await asyncio.wait_for(self._sessions.get(), timeout=1)


PROVIDER_HANDSHAKE_LIMITS: dict[str, object] = {
    "max_frame_bytes": 67_108_864,
    "provider_pending_callbacks": 32,
    "provider_callback_timeout_seconds": 30,
    "provider_lease_seconds": 30,
    "provider_mutations_per_terminal": 1,
}


@asynccontextmanager
async def provider_callback_server(
    socket_path: Path,
    *,
    client_id: str,
    provider_id: str,
    credential: str,
    make_session: Callable[[asyncio.StreamReader, asyncio.StreamWriter], object],
    limits: Mapping[str, object] | None = None,
    result_extra: Mapping[str, object] | None = None,
    check_request: Callable[[object], object] | None = None,
) -> AsyncIterator[asyncio.Queue]:
    """Answer one provider callback handshake per connection and queue the open session."""
    sessions: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    tasks: set[asyncio.Task[None]] = set()
    writers: set[asyncio.StreamWriter] = set()
    failures: list[BaseException] = []
    handshake_limits = {**PROVIDER_HANDSHAKE_LIMITS, **(limits or {})}

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        writers.add(writer)
        try:
            request = json.loads(await asyncio.wait_for(reader.readline(), timeout=1))
            if check_request is not None:
                check_request(request)
            assert request == {
                "id": 1,
                "method": "frontend.handshake",
                "params": {
                    "api": {"major": 1, "minor": 1},
                    "client_id": client_id,
                    "role": "provider",
                    "channel": "callback",
                    "required_capabilities": ["terminal-provider.v1"],
                    "provider_id": provider_id,
                    "provider_credential": credential,
                },
            }
            response = {
                "id": 1,
                "ok": True,
                "result": {
                    "api": {"major": 1, "minor": 1},
                    "daemon_instance_id": "fixture-daemon",
                    "package_version": "0.0.0-test",
                    "capabilities": ["terminal-provider.v1"],
                    "limits": handshake_limits,
                    "provider_generation": 7,
                    **(result_extra or {}),
                },
            }
            writer.write(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
            await writer.drain()
            await sessions.put(make_session(reader, writer))
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
        yield sessions
    finally:
        stop.set()
        server.close()
        await server.wait_closed()
        for writer in tuple(writers):
            writer.close()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=1)
        socket_path.unlink(missing_ok=True)
        if failures:
            raise failures[0]


@asynccontextmanager
async def fixture_daemon(
    *, limits: Mapping[str, object] | None = None
) -> AsyncIterator[FixtureDaemon]:
    root = Path(tempfile.mkdtemp(prefix="provider-fixture-", dir="/tmp"))
    socket_path = root / "frontend.sock"
    try:
        async with provider_callback_server(
            socket_path,
            client_id="provider-fixture",
            provider_id="provider-a",
            credential="credential-a",
            make_session=CallbackSession,
            limits=limits,
            result_extra={"future_handshake_field": {"kept": True}},
        ) as sessions:
            yield FixtureDaemon(socket_path, sessions)
    finally:
        shutil.rmtree(root)


def provider_client(socket_path: Path, handlers: Mapping[str, Handler]) -> ProviderClient:
    return ProviderClient(
        socket_path,
        client_id="provider-fixture",
        provider_id="provider-a",
        provider_credential="credential-a",
        handlers=handlers,
    )


async def connect_provider(client: ProviderClient, daemon: FixtureDaemon) -> CallbackSession:
    connect = asyncio.create_task(client.connect())
    session = await daemon.next_session()
    result = await connect
    assert result.provider_generation == 7
    return session


async def eventually(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0.001)


def mapping_value(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    nested = value[key]
    assert isinstance(nested, Mapping)
    return nested


def frame_id(value: Mapping[str, object]) -> str:
    callback_id = value["id"]
    assert isinstance(callback_id, str)
    return callback_id


def error_code(value: Mapping[str, object]) -> str:
    code = mapping_value(value, "error")["code"]
    assert isinstance(code, str)
    return code


def inventory_frame(callback_id: str, *, generation: int = 7) -> dict[str, object]:
    return {
        "type": "request",
        "id": callback_id,
        "method": "terminal.inventory",
        "params": {"provider_generation": generation},
    }


def inspect_frame(callback_id: str, *, generation: int = 7) -> dict[str, object]:
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


def deliver_frame(
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


def create_frame(
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


def result_for(request: CallbackRequest) -> dict[str, object]:
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
