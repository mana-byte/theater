"""Public SDK lanes, decoding, timeouts and reconnects against a scripted daemon."""

from __future__ import annotations

import asyncio
import tempfile
import threading
from pathlib import Path

import pytest

from tests.support.frontend_wire import read_request
from tests.support.sdk_fixture_server import fixture_server, send_frame
from tests.support.sdk_state_frames import operation_frame, state_handshake
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
from theater.frontend.transport import FrontendTransport, TransportBusy


async def _handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    capabilities: list[str],
    first: bool = True,
    minor: int = 1,
) -> dict[str, object]:
    request = await read_request(reader)
    assert request is not None
    assert request["id"] == 1 or not first
    assert request["method"] == "frontend.handshake"
    params = request["params"]
    assert isinstance(params, dict)
    assert params["api"] == {"major": 1, "minor": 1}
    await send_frame(
        writer,
        {
            "id": request["id"],
            "ok": True,
            "result": {
                "api": {"major": 1, "minor": minor},
                "daemon_instance_id": "fixture-daemon",
                "package_version": "0.0.0-test",
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
        request = await read_request(reader)
        assert request is not None
        await send_frame(writer, {"id": request["id"], "ok": True, "result": {"records": []}})

    async with (
        fixture_server(serve_once) as socket_path,
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
        request = await read_request(reader)
        assert request is not None
        await send_frame(
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
        fixture_server(handler) as socket_path,
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


async def test_sdk_uses_dedicated_lanes_and_preserves_future_values() -> None:
    sessions: list[list[str]] = []
    capabilities = ["contract.v1", "orchestration.v1", "state.follow.v1"]

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handshake(reader, writer, capabilities=capabilities)
        methods: list[str] = []
        while request := await read_request(reader):
            method = request["method"]
            assert isinstance(method, str)
            methods.append(method)
            request_id = request["id"]
            assert isinstance(request_id, int)
            if method == "frontend.participants.get":
                await send_frame(
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
                await send_frame(
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
                await send_frame(
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

    async with fixture_server(handler) as socket_path:
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


async def test_sdk_enforces_local_catalog_rules_and_preserves_unknown_refusals() -> None:
    requests: list[str] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handshake(reader, writer, capabilities=["contract.v1"])
        request = await read_request(reader)
        assert request is not None
        requests.append(str(request["method"]))
        await send_frame(
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

    async with fixture_server(handler) as socket_path:
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


async def test_sdk_tolerates_unknown_owner_kinds_without_masking_bad_owner_values() -> None:
    owners = {
        "future-owner": {"kind": "future_delegate", "participant_id": "owner-x", "revision": 1},
        "future-owner-without-participant": {"kind": "future_delegate", "revision": 2},
        "malformed-owner": {"kind": "future_delegate", "participant_id": 7, "revision": 3},
    }

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handshake(reader, writer, capabilities=["orchestration.v1"])
        while request := await read_request(reader):
            params = request["params"]
            assert isinstance(params, dict)
            participant_id = params["participant_id"]
            assert isinstance(participant_id, str)
            await send_frame(
                writer,
                {
                    "id": request["id"],
                    "ok": True,
                    "result": _participant_result(owners[participant_id]),
                },
            )

    async with fixture_server(handler) as socket_path:
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


async def test_recall_read_omits_optional_offsets_and_preserves_explicit_values() -> None:
    requests: list[dict[str, object]] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handshake(reader, writer, capabilities=["observation.v1"])
        while request := await read_request(reader):
            requests.append(request)
            await send_frame(writer, {"id": request["id"], "ok": True, "result": {}})

    async with fixture_server(handler) as socket_path:
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


async def test_usage_by_participant_sends_filters_and_freezes_result() -> None:
    requests: list[dict[str, object]] = []
    result = {
        "since": 10.0,
        "truncated": False,
        "participants": [
            {
                "participant_id": "participant-a",
                "harness": "codex",
                "models": ["gpt-5"],
                "input_tokens": 12,
                "output_tokens": 4,
                "cache_creation_input_tokens": 3,
                "cache_read_input_tokens": 2,
                "reasoning_output_tokens": 1,
                "cost_microcents": 50,
                "first_at": 10.0,
                "last_at": 20.0,
            }
        ],
    }

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handshake(reader, writer, capabilities=["diagnostics.v1"])
        request = await read_request(reader)
        assert request is not None
        requests.append(request)
        await send_frame(writer, {"id": request["id"], "ok": True, "result": result})

    async with fixture_server(handler) as socket_path:
        client = FrontendClient(socket_path, client_id="sdk-usage")
        response = await client.usage.by_participant(
            since=10.0,
            participant_ids=("participant-a", "participant-b"),
            limit=2,
        )
        await client.close()

    assert requests == [
        {
            "id": 2,
            "method": "frontend.usage.by_participant",
            "params": {
                "since": 10.0,
                "participant_ids": ["participant-a", "participant-b"],
                "limit": 2,
            },
        }
    ]
    assert response.value["participants"][0]["models"] == ("gpt-5",)


async def test_participant_spawn_omits_an_empty_prompt_from_the_wire() -> None:
    requests: list[dict[str, object]] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handshake(reader, writer, capabilities=["orchestration.v1"])
        request = await read_request(reader)
        assert request is not None
        requests.append(request)
        await send_frame(
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

    async with fixture_server(handler) as socket_path:
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


async def test_timeout_retires_only_the_operation_wait_lane_without_replay() -> None:
    operation_closed = asyncio.Event()
    interactive_methods: list[str] = []
    operation_calls = 0
    capabilities = ["contract.v1", "orchestration.v1"]

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal operation_calls
        await _handshake(reader, writer, capabilities=capabilities)
        request = await read_request(reader)
        assert request is not None
        method = request["method"]
        assert isinstance(method, str)
        if method == "frontend.operations.await":
            operation_calls += 1
            assert await read_request(reader) is None
            operation_closed.set()
            return
        assert method == "frontend.contract.get"
        interactive_methods.append(method)
        await send_frame(writer, {"id": request["id"], "ok": True, "result": {}})
        while request := await read_request(reader):
            assert request["method"] == "frontend.contract.get"
            interactive_methods.append("frontend.contract.get")
            await send_frame(writer, {"id": request["id"], "ok": True, "result": {}})

    async with fixture_server(handler) as socket_path:
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


async def test_busy_ordinary_lane_does_not_cancel_the_request_already_in_flight() -> None:
    first_received = asyncio.Event()
    release_first = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handshake(reader, writer, capabilities=["contract.v1"])
        request = await read_request(reader)
        assert request is not None
        assert request["method"] == "frontend.contract.get"
        first_received.set()
        await release_first.wait()
        await send_frame(writer, {"id": request["id"], "ok": True, "result": {}})

    async with fixture_server(handler) as socket_path:
        client = FrontendClient(socket_path, client_id="sdk-busy")
        first = asyncio.create_task(client.contract.get())
        await asyncio.wait_for(first_received.wait(), timeout=1)
        with pytest.raises(TransportBusy):
            await client.contract.get()
        release_first.set()
        assert (await first).value == {}
        await client.close()


async def test_an_older_minor_daemon_stays_usable_but_refuses_newer_methods_locally() -> None:
    """A 1.0 daemon serves 1.0 methods; 1.1-only methods fail before any frame is sent."""
    methods: list[object] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handshake(reader, writer, capabilities=["diagnostics.v1"], minor=0)
        while (request := await read_request(reader)) is not None:
            methods.append(request["method"])
            await send_frame(writer, {"id": request["id"], "ok": True, "result": {}})

    async with fixture_server(handler) as socket_path:
        client = FrontendClient(socket_path, client_id="sdk-older-minor")
        assert (await client.usage.totals()).value == {}
        with pytest.raises(CapabilityUnavailable, match=r"requires public API 1\.1"):
            await client.usage.by_participant()
        await client.close()

    assert methods == ["frontend.usage.totals"]


async def test_operation_wait_cancellation_detaches_and_handle_survives_reconnect() -> None:
    first_wait_started = asyncio.Event()
    first_wait_closed = asyncio.Event()
    operation_waits = 0
    methods: list[str] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal operation_waits
        await state_handshake(reader, writer)
        while request := await read_request(reader):
            method = request["method"]
            methods.append(str(method))
            request_id = request["id"]
            assert type(request_id) is int
            if method == "frontend.operations.await":
                operation_waits += 1
                if operation_waits == 1:
                    first_wait_started.set()
                    assert await read_request(reader) is None
                    first_wait_closed.set()
                    return
                if operation_waits == 2:
                    result = {
                        "operation": operation_frame("running", "delivering"),
                        "timed_out": True,
                    }
                else:
                    result = {
                        "operation": operation_frame("succeeded", "settled"),
                        "timed_out": False,
                    }
                await send_frame(writer, {"id": request_id, "ok": True, "result": result})
            elif method == "frontend.operations.get":
                await send_frame(
                    writer,
                    {
                        "id": request_id,
                        "ok": True,
                        "result": operation_frame("running", "delivering"),
                    },
                )
            else:
                raise AssertionError(f"unexpected method {method!r}")

    async with fixture_server(handler) as socket_path:
        client = FrontendClient(socket_path, client_id="sdk-follow")
        waiting = asyncio.create_task(client.operations.wait("operation-a", wait_seconds=30))
        await asyncio.wait_for(first_wait_started.wait(), timeout=1)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await asyncio.wait_for(first_wait_closed.wait(), timeout=1)

        observed = await client.operations.get("operation-a")
        assert observed.value.state == "running"
        assert observed.value.job_handle == "job-a"

        timed_out = await client.operations.wait("operation-a", wait_seconds=0)
        assert timed_out.value.timed_out is True
        assert timed_out.value.operation.state == "running"

        settled = await client.operations.wait("operation-a", wait_seconds=0)
        assert settled.value.timed_out is False
        assert settled.value.operation.state == "succeeded"
        assert settled.value.operation.job_handle == "job-a"
        await client.close()

    assert methods.count("frontend.operations.await") == 3
    assert "frontend.jobs.await" not in methods


async def test_transport_is_disconnected_once_the_peer_hangs_up():
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        path = Path(directory) / "daemon.sock"

        async def hang_up(_reader, writer):
            writer.close()

        server = await asyncio.start_unix_server(hang_up, path)
        transport = FrontendTransport(path)
        try:
            await transport.connect()
            for _ in range(100):
                if not transport.connected:
                    break
                await asyncio.sleep(0.01)
            assert not transport.connected
        finally:
            transport.abort()
            server.close()
            await server.wait_closed()
