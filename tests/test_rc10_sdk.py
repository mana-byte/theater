"""Focused independent checks for the RC10 public SDK lanes and decoding."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import tempfile
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from theater.frontend import (
    CapabilityUnavailable,
    ClientStateError,
    ConnectionRole,
    FrontendClient,
    FrontendError,
    IdempotencyRequirementError,
    MethodRoleError,
    RequestTimedOut,
    ResponseValidationError,
)
from theater.frontend.transport import TransportBusy

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


@asynccontextmanager
async def _fixture_server(handler: Handler) -> AsyncIterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="r10sdk-", dir="/tmp"))
    path = root / "frontend.sock"
    writers: set[asyncio.StreamWriter] = set()
    tasks: set[asyncio.Task[None]] = set()
    failures: list[BaseException] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        writers.add(writer)
        try:
            await handler(reader, writer)
        except BaseException as exc:
            failures.append(exc)
        finally:
            writers.discard(writer)
            tasks.discard(task)
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()

    server = await asyncio.start_unix_server(serve, path=str(path))
    try:
        yield path
    finally:
        server.close()
        await server.wait_closed()
        for writer in tuple(writers):
            writer.close()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=1)
        path.unlink(missing_ok=True)
        shutil.rmtree(root)
        if failures:
            raise failures[0]


async def _read_request(reader: asyncio.StreamReader) -> dict[str, object] | None:
    frame = await reader.readline()
    if not frame:
        return None
    assert frame.endswith(b"\n")
    value = json.loads(frame)
    assert isinstance(value, dict)
    return value


async def _send_response(writer: asyncio.StreamWriter, value: dict[str, object]) -> None:
    writer.write(json.dumps(value, separators=(",", ":")).encode("utf-8") + b"\n")
    await writer.drain()


async def _handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    capabilities: list[str],
    first: bool = True,
) -> dict[str, object]:
    request = await _read_request(reader)
    assert request is not None
    assert request["id"] == 1 or not first
    assert request["method"] == "frontend.handshake"
    params = request["params"]
    assert isinstance(params, dict)
    assert params["api"] == {"major": 1, "minor": 0}
    await _send_response(
        writer,
        {
            "id": request["id"],
            "ok": True,
            "result": {
                "api": {"major": 1, "minor": 0},
                "daemon_instance_id": "fixture-daemon",
                "package_version": "1.0.0rc10",
                "capabilities": capabilities,
                "limits": {"max_frame_bytes": 67_108_864, "max_in_flight": 1},
                "future_handshake_field": {"kept": True},
            },
            "future_handshake_envelope": "kept",
        },
    )
    return request


def _participant_result(owner: dict[str, object]) -> dict[str, object]:
    return {
        "participant_id": "participant-a",
        "origin": "spawned",
        "harness": "codex",
        "status": "idle",
        "owner": owner,
        "addressable": True,
        "presence": "unknown",
        "actions": {},
    }


async def test_client_replaces_a_lane_the_daemon_hung_up_on() -> None:
    connections = 0

    async def serve_once(reader, writer):
        nonlocal connections
        connections += 1
        await _handshake(reader, writer, capabilities=["trajectory.v1"], first=False)
        request = await _read_request(reader)
        assert request is not None
        await _send_response(writer, {"id": request["id"], "ok": True, "result": {"records": []}})

    async with (
        _fixture_server(serve_once) as socket_path,
        FrontendClient(socket_path, client_id="sdk-reconnect") as client,
    ):
        await client.trajectory.snapshot("p")
        for _ in range(100):
            if not client.connected:
                break
            await asyncio.sleep(0.01)
        assert (await client.trajectory.snapshot("p")).value["records"] == ()
    assert connections == 2


@pytest.mark.parametrize("outcome", ["valid", "invalid", "cancelled"])
async def test_bulk_response_validation_runs_off_loop_and_still_refuses_invalid_data(
    monkeypatch, outcome
):
    async def handler(reader, writer):
        await _handshake(reader, writer, capabilities=["trajectory.v1"])
        request = await _read_request(reader)
        assert request is not None
        await _send_response(
            writer,
            {
                "id": request["id"],
                "ok": True,
                "result": {"records": [None] * 501} if outcome == "invalid" else {"records": []},
            },
        )

    original = FrontendClient._decode_response
    loop_thread = threading.get_ident()
    loop = asyncio.get_running_loop()
    decoding = asyncio.Event()
    release = threading.Event()
    threads = []

    def decoded(self, method, value, request_id):
        if method == "frontend.trajectory.snapshot":
            threads.append(threading.get_ident())
            loop.call_soon_threadsafe(decoding.set)
            assert release.wait(timeout=5)
        return original(self, method, value, request_id)

    monkeypatch.setattr(FrontendClient, "_decode_response", decoded)
    async with (
        _fixture_server(handler) as socket_path,
        FrontendClient(socket_path, client_id="sdk-bulk") as client,
    ):
        pending = asyncio.create_task(client.trajectory.snapshot("p"))
        try:
            await asyncio.wait_for(decoding.wait(), timeout=2)
            with pytest.raises(TransportBusy):
                await client.trajectory.snapshot("p")
            if outcome == "cancelled":
                pending.cancel()
        finally:
            release.set()
        if outcome == "invalid":
            with pytest.raises(ResponseValidationError):
                await pending
            assert not client.connected
        elif outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert not client.connected
        else:
            response = await pending
            assert response.value["records"] == ()
    assert len(threads) == 1 and threads[0] != loop_thread


@pytest.mark.asyncio
async def test_sdk_uses_dedicated_lanes_and_preserves_future_values() -> None:
    sessions: list[list[str]] = []
    capabilities = ["contract.v1", "orchestration.v1", "state.follow.v1"]

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handshake(reader, writer, capabilities=capabilities)
        methods: list[str] = []
        while request := await _read_request(reader):
            method = request["method"]
            assert isinstance(method, str)
            methods.append(method)
            request_id = request["id"]
            assert isinstance(request_id, int)
            if method == "frontend.participants.get":
                await _send_response(
                    writer,
                    {
                        "id": request_id,
                        "ok": True,
                        "result": {
                            "participant_id": "participant-a",
                            "origin": "spawned",
                            "harness": "codex",
                            "status": "future-status",
                            "owner": {"kind": "local_operator", "revision": 1},
                            "addressable": True,
                            "presence": "unknown",
                            "actions": {},
                            "future_participant_field": ["kept"],
                        },
                        "future_envelope_field": {"kept": True},
                    },
                )
            elif method == "frontend.operations.await":
                await _send_response(
                    writer,
                    {
                        "id": request_id,
                        "ok": True,
                        "result": {
                            "operation": {
                                "operation_id": "operation-a",
                                "kind": "send",
                                "state": "paused-by-provider",
                                "phase": "future-phase",
                                "actor": {"client_id": "sdk-a"},
                                "target_ids": ["participant-a"],
                                "future_operation_field": "kept",
                            },
                            "timed_out": False,
                        },
                    },
                )
            elif method == "frontend.state.follow":
                await _send_response(
                    writer,
                    {
                        "id": request_id,
                        "ok": True,
                        "result": {
                            "transactions": [
                                {
                                    "transaction_id": "tx-a",
                                    "events": [
                                        {
                                            "kind": "future.entity_changed",
                                            "entity_id": "participant-a",
                                            "entity_revision": 2,
                                            "payload": {},
                                            "future_event_field": "kept",
                                        }
                                    ],
                                    "ending_cursor": {"stream_id": "stream-a", "sequence": 2},
                                }
                            ],
                            "cursor": {"stream_id": "stream-a", "sequence": 2},
                            "timed_out": False,
                        },
                    },
                )
            else:
                raise AssertionError(f"unexpected SDK method {method}")
        sessions.append(methods)

    async with _fixture_server(handler) as socket_path:
        client = FrontendClient(
            socket_path,
            client_id="sdk-a",
            required_capabilities=("orchestration.v1", "state.follow.v1"),
        )
        participant = await client.participants.get("participant-a")
        operation = await client.operations.await_("operation-a", wait_seconds=1)
        follow = await client.state.follow({"stream_id": "stream-a", "sequence": 1})
        assert client.handshake_result is not None
        assert client.handshake_result.extra["future_handshake_field"] == {"kept": True}
        assert client.handshake_response is not None
        assert client.handshake_response.extra["future_handshake_envelope"] == "kept"
        assert participant.value.status == "future-status"
        assert participant.value.extra["future_participant_field"] == ("kept",)
        assert participant.extra["future_envelope_field"] == {"kept": True}
        assert operation.value.operation.known_state is None
        assert operation.value.operation.extra["future_operation_field"] == "kept"
        assert not follow.value.transactions[0].events[0].known_kind
        assert follow.value.transactions[0].events[0].extra["future_event_field"] == "kept"
        await client.close()

    assert sorted(sessions) == [
        ["frontend.operations.await"],
        ["frontend.participants.get"],
        ["frontend.state.follow"],
    ]


@pytest.mark.asyncio
async def test_sdk_enforces_local_catalog_rules_and_preserves_unknown_refusals() -> None:
    requests: list[str] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handshake(reader, writer, capabilities=["contract.v1"])
        request = await _read_request(reader)
        assert request is not None
        requests.append(str(request["method"]))
        await _send_response(
            writer,
            {
                "id": request["id"],
                "ok": False,
                "error": {
                    "code": "future_refusal",
                    "message": "A newer daemon refused this request.",
                    "future_error_field": ["kept"],
                },
                "future_envelope_field": {"kept": True},
            },
        )

    async with _fixture_server(handler) as socket_path:
        invalid_handshake = FrontendClient(
            socket_path,
            client_id="sdk-invalid-handshake",
            required_capabilities=("contract.v1", "contract.v1"),
        )
        with pytest.raises(ClientStateError):
            await invalid_handshake.connect()
        assert not invalid_handshake.connected

        no_socket_client = FrontendClient(socket_path, client_id="sdk-local")
        with pytest.raises(IdempotencyRequirementError):
            await no_socket_client.participants.update("participant-a", idempotency_key="")
        provider_client = FrontendClient(
            socket_path,
            client_id="provider-local",
            role=ConnectionRole.PROVIDER,
            provider_id="provider-a",
            provider_credential="credential-a",
        )
        with pytest.raises(MethodRoleError):
            await provider_client.participants.get("participant-a")

        client = FrontendClient(socket_path, client_id="sdk-error")
        await client.connect()
        with pytest.raises(CapabilityUnavailable):
            await client.participants.get("participant-a")
        with pytest.raises(FrontendError) as raised:
            await client.contract.get()
        assert raised.value.value.code == "future_refusal"
        assert raised.value.value.details == {}
        assert raised.value.value.extra["future_error_field"] == ("kept",)
        assert raised.value.response.extra["future_envelope_field"] == {"kept": True}
        await client.close()

    assert requests == ["frontend.contract.get"]


@pytest.mark.asyncio
async def test_sdk_tolerates_unknown_owner_kinds_without_masking_bad_owner_values() -> None:
    owners = {
        "future-owner": {"kind": "future_delegate", "participant_id": "owner-x", "revision": 1},
        "future-owner-without-participant": {"kind": "future_delegate", "revision": 2},
        "malformed-owner": {"kind": "future_delegate", "participant_id": 7, "revision": 3},
    }

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handshake(reader, writer, capabilities=["orchestration.v1"])
        while request := await _read_request(reader):
            params = request["params"]
            assert isinstance(params, dict)
            participant_id = params["participant_id"]
            assert isinstance(participant_id, str)
            await _send_response(
                writer,
                {
                    "id": request["id"],
                    "ok": True,
                    "result": _participant_result(owners[participant_id]),
                },
            )

    async with _fixture_server(handler) as socket_path:
        client = FrontendClient(socket_path, client_id="sdk-future-owner")
        with_participant = await client.participants.get("future-owner")
        without_participant = await client.participants.get("future-owner-without-participant")
        with pytest.raises(ResponseValidationError):
            await client.participants.get("malformed-owner")
        assert with_participant.value.owner.kind == "future_delegate"
        assert with_participant.value.owner.participant_id == "owner-x"
        assert without_participant.value.owner.kind == "future_delegate"
        assert without_participant.value.owner.participant_id is None
        await client.close()


@pytest.mark.asyncio
async def test_recall_read_omits_optional_offsets_and_preserves_explicit_values() -> None:
    requests: list[dict[str, object]] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handshake(reader, writer, capabilities=["observation.v1"])
        while request := await _read_request(reader):
            requests.append(request)
            await _send_response(writer, {"id": request["id"], "ok": True, "result": {}})

    async with _fixture_server(handler) as socket_path:
        client = FrontendClient(socket_path, client_id="sdk-recall")
        assert (await client.recall.read("segment-default")).value == {}
        assert (await client.recall.read("segment-explicit", offset=4, max_bytes=128)).value == {}
        await client.close()

    assert requests == [
        {"id": 2, "method": "frontend.recall.read", "params": {"segment_id": "segment-default"}},
        {
            "id": 3,
            "method": "frontend.recall.read",
            "params": {"segment_id": "segment-explicit", "offset": 4, "max_bytes": 128},
        },
    ]


@pytest.mark.asyncio
async def test_participant_spawn_omits_an_empty_prompt_from_the_wire() -> None:
    requests: list[dict[str, object]] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handshake(reader, writer, capabilities=["orchestration.v1"])
        request = await _read_request(reader)
        assert request is not None
        requests.append(request)
        await _send_response(
            writer,
            {
                "id": request["id"],
                "ok": True,
                "result": {
                    "operation_id": "operation-a",
                    "state": "accepted",
                    "participant_id": "participant-a",
                    "job_handle": "participant-a",
                },
            },
        )

    async with _fixture_server(handler) as socket_path:
        client = FrontendClient(socket_path, client_id="sdk-promptless-spawn")
        await client.participants.spawn(
            "codex",
            "",
            "manual",
            cwd="/workspace",
            idempotency_key="spawn-without-prompt",
        )
        await client.close()

    assert requests[0]["method"] == "frontend.participants.spawn"
    params = requests[0]["params"]
    assert isinstance(params, dict)
    assert "prompt" not in params


@pytest.mark.asyncio
async def test_timeout_retires_only_the_operation_wait_lane_without_replay() -> None:
    operation_closed = asyncio.Event()
    interactive_methods: list[str] = []
    operation_calls = 0
    capabilities = ["contract.v1", "orchestration.v1"]

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal operation_calls
        await _handshake(reader, writer, capabilities=capabilities)
        request = await _read_request(reader)
        assert request is not None
        method = request["method"]
        assert isinstance(method, str)
        if method == "frontend.operations.await":
            operation_calls += 1
            assert await _read_request(reader) is None
            operation_closed.set()
            return
        assert method == "frontend.contract.get"
        interactive_methods.append(method)
        await _send_response(writer, {"id": request["id"], "ok": True, "result": {}})
        while request := await _read_request(reader):
            assert request["method"] == "frontend.contract.get"
            interactive_methods.append("frontend.contract.get")
            await _send_response(writer, {"id": request["id"], "ok": True, "result": {}})

    async with _fixture_server(handler) as socket_path:
        client = FrontendClient(
            socket_path,
            client_id="sdk-timeout",
            required_capabilities=("contract.v1", "orchestration.v1"),
            request_timeout=0.05,
        )
        await client.contract.get()
        with pytest.raises(RequestTimedOut):
            await client.operations.await_("operation-a", wait_seconds=30)
        await asyncio.wait_for(operation_closed.wait(), timeout=1)
        await client.contract.get()
        await client.close()

    assert operation_calls == 1
    assert interactive_methods == ["frontend.contract.get", "frontend.contract.get"]


@pytest.mark.asyncio
async def test_busy_ordinary_lane_does_not_cancel_the_request_already_in_flight() -> None:
    first_received = asyncio.Event()
    release_first = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handshake(reader, writer, capabilities=["contract.v1"])
        request = await _read_request(reader)
        assert request is not None
        assert request["method"] == "frontend.contract.get"
        first_received.set()
        await release_first.wait()
        await _send_response(writer, {"id": request["id"], "ok": True, "result": {}})

    async with _fixture_server(handler) as socket_path:
        client = FrontendClient(socket_path, client_id="sdk-busy")
        first = asyncio.create_task(client.contract.get())
        await asyncio.wait_for(first_received.wait(), timeout=1)
        with pytest.raises(TransportBusy):
            await client.contract.get()
        release_first.set()
        assert (await first).value == {}
        await client.close()
