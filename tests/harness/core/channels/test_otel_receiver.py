"""The native OTLP loopback receiver: ingestion, authentication, bounds and overload."""

from __future__ import annotations

import asyncio
import json
import stat
import threading
from dataclasses import replace

import pytest

from tests.support.otel_channel_rig import (
    endpoint_parts,
    otel_manifest,
    otel_payload,
    otel_record,
    post,
    prepare,
)
from theater.constants.harness import (
    HARNESS_CHANNEL_IDENTIFIER_MAX_CHARS,
    HARNESS_OTEL_PARSE_MAX_IN_FLIGHT,
)
from theater.daemon.server import Daemon
from theater.harness.channels import CompositeSource
from theater.harness.channels.otel import NativeOtelRuntime
from theater.harness.channels.otel import runtime as otel_runtime
from theater.harness.channels.otel.receiver import (
    NativeOtelReceiver,
    OtelHttpError,
    OtelHttpResponse,
)
from theater.harness.contracts.channels import OtelProtocol
from theater.harness.manifests.compiler import compile_manifest


async def _malformed_status(endpoint: str) -> int:
    host, port = endpoint_parts(endpoint)
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(b"not an HTTP request\r\n\r\n")
    await writer.drain()
    status = (await reader.readline()).decode().split()
    writer.close()
    await writer.wait_closed()
    return int(status[1])


async def _raw_status(endpoint: str, request: bytes) -> int:
    host, port = endpoint_parts(endpoint)
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(request)
    await writer.drain()
    status = (await reader.readline()).decode().split()
    writer.close()
    await writer.wait_closed()
    return int(status[1])


@pytest.mark.asyncio
async def test_native_otel_loopback_ingests_multiple_records_and_retries(
    theater_home, tmp_path
) -> None:
    harness = compile_manifest("acme", otel_manifest(max_queue=3, max_records=3))
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant, credential = await prepare(daemon, harness, tmp_path)
    try:
        assert stat.S_IMODE(credential.token_path.stat().st_mode) == 0o600
        assert (
            credential.token not in credential.token_path.with_name("native-otel.json").read_text()
        )
        payload = otel_payload(
            participant,
            [
                otel_record(native_id="one", export_id="export-a"),
                otel_record(native_id="two", export_id="export-a"),
            ],
        )
        status, headers, body = await post(
            daemon.otel_runtime.endpoint, payload, token=credential.token
        )
        assert (status, headers["content-type"], body) == (200, "application/json", b"{}")
        (health,) = daemon.otel_runtime.health_snapshot(participant.id)
        assert health.channel_id == "native-otel"
        assert health.accepted == 2
        assert health.last_success_at is not None
        assert credential.token not in str(health)
        source = daemon.observer._open_source(participant.id, harness.observer)
        assert isinstance(source, CompositeSource)
        batch = await source.read()
        assert [fact.native_id for fact in batch.trajectory] == ["one", "two"]
        assert batch.progressed is False
        retry = await post(daemon.otel_runtime.endpoint, payload, token=credential.token)
        assert retry[0] == 200
        assert (await source.read()).trajectory == ()
        otel_source = source._enrichments[0].source
        assert otel_source.channel_health().state.value == "healthy"
        await source.aclose()
    finally:
        await daemon.aclose()


@pytest.mark.asyncio
async def test_native_otel_protobuf_fixture_uses_otlp_response_semantics(
    theater_home, tmp_path
) -> None:
    from google.protobuf.json_format import ParseDict
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
        ExportLogsServiceRequest,
        ExportLogsServiceResponse,
    )

    harness = compile_manifest(
        "acme", otel_manifest(protocol=OtelProtocol.OTLP_HTTP_PROTOBUF, max_queue=1, max_records=2)
    )
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant, credential = await prepare(daemon, harness, tmp_path)
    try:
        request = ExportLogsServiceRequest()
        records = [otel_record(native_id="protobuf", export_id="proto")]
        records[0]["traceId"] = "dHJhY2U="
        records[0]["spanId"] = "c3Bhbg=="
        ParseDict(
            json.loads(otel_payload(participant, records)),
            request,
        )
        status, headers, body = await post(
            daemon.otel_runtime.endpoint,
            request.SerializeToString(),
            token=credential.token,
            content_type="application/x-protobuf",
        )
        assert (status, headers["content-type"], body) == (200, "application/x-protobuf", b"")
        rejected = await post(
            daemon.otel_runtime.endpoint,
            request.SerializeToString(),
            token="forged",
            content_type="application/x-protobuf",
        )
        from google.rpc.status_pb2 import Status

        error = Status()
        error.ParseFromString(rejected[2])
        assert rejected[0] == 401
        assert rejected[1]["content-type"] == "application/x-protobuf"
        assert error.code == 16
        assert error.message == "native OTel credential is invalid"
        source = daemon.observer._open_source(participant.id, harness.observer)
        assert source is not None
        assert [fact.native_id for fact in (await source.read()).trajectory] == ["protobuf"]
        overflow = ExportLogsServiceRequest()
        overflow_records = [
            otel_record(native_id="overflow-one", export_id="protobuf-overflow"),
            otel_record(native_id="overflow-two", export_id="protobuf-overflow"),
        ]
        for record in overflow_records:
            record["traceId"] = "dHJhY2U="
            record["spanId"] = "c3Bhbg=="
        ParseDict(
            json.loads(
                otel_payload(
                    participant,
                    overflow_records,
                )
            ),
            overflow,
        )
        partial = await post(
            daemon.otel_runtime.endpoint,
            overflow.SerializeToString(),
            token=credential.token,
            content_type="application/x-protobuf",
        )
        partial_response = ExportLogsServiceResponse()
        partial_response.ParseFromString(partial[2])
        assert partial[0] == 200
        assert partial[1]["content-type"] == "application/x-protobuf"
        assert partial_response.partial_success.rejected_log_records == 1
        assert partial_response.partial_success.error_message == "native OTel inbox overflow"
        wrong_resource = ExportLogsServiceRequest()
        ParseDict(
            json.loads(otel_payload(participant, records, harness="other")),
            wrong_resource,
        )
        forbidden = await post(
            daemon.otel_runtime.endpoint,
            wrong_resource.SerializeToString(),
            token=credential.token,
            content_type="application/x-protobuf",
        )
        error.ParseFromString(forbidden[2])
        assert forbidden[0] == 403
        assert forbidden[1]["content-type"] == "application/x-protobuf"
        assert error.code == 7
        await source.aclose()
    finally:
        await daemon.aclose()


@pytest.mark.asyncio
async def test_native_otel_rejects_auth_identity_path_type_and_bounds(
    theater_home, tmp_path
) -> None:
    harness = compile_manifest("acme", otel_manifest(max_queue=2, max_records=2))
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant, credential = await prepare(daemon, harness, tmp_path)
    try:
        payload = otel_payload(participant, [otel_record(native_id="one", export_id="one")])
        assert await _malformed_status(daemon.otel_runtime.endpoint) == 400
        missing = await post(daemon.otel_runtime.endpoint, payload)
        assert missing[0] == 401
        assert json.loads(missing[2]) == {
            "code": 16,
            "message": "native OTel credential is invalid",
        }
        rejected = await post(daemon.otel_runtime.endpoint, payload, token="forged")
        assert rejected[0] == 401
        wrong_harness = otel_payload(
            participant, [otel_record(native_id="one", export_id="two")], harness="other"
        )
        assert (await post(daemon.otel_runtime.endpoint, wrong_harness, token=credential.token))[
            0
        ] == 403
        wrong_path = await post(
            daemon.otel_runtime.endpoint, payload, token=credential.token, path="/other"
        )
        assert wrong_path[0] == 404 and json.loads(wrong_path[2])["code"] == 5
        wrong_method = await post(
            daemon.otel_runtime.endpoint,
            payload,
            token=credential.token,
            method="GET",
        )
        assert wrong_method[0] == 405 and json.loads(wrong_method[2])["code"] == 12
        assert (
            await _raw_status(
                daemon.otel_runtime.endpoint,
                b"GET /v1/logs HTTP/1.1\r\nHost: loopback\r\n\r\n",
            )
            == 405
        )
        assert (
            await _raw_status(
                daemon.otel_runtime.endpoint,
                b"POST /v1/logs HTTP/1.1\r\nBad Header: value\r\n\r\n",
            )
            == 400
        )
        typed = await post(
            daemon.otel_runtime.endpoint,
            payload,
            token=credential.token,
            content_type="text/plain",
        )
        assert typed[0] == 415 and typed[1]["content-type"] == "application/json"
        overflow = otel_payload(
            participant,
            [
                otel_record(native_id="one", export_id="batch"),
                otel_record(native_id="two", export_id="batch"),
                otel_record(native_id="three", export_id="batch"),
            ],
        )
        assert (await post(daemon.otel_runtime.endpoint, overflow, token=credential.token))[
            0
        ] == 400
        source = daemon.observer._open_source(participant.id, harness.observer)
        assert source is not None
        health = source._enrichments[0].source.channel_health()
        assert health is not None and health.state.value == "degraded"
        await source.aclose()
    finally:
        await daemon.aclose()


@pytest.mark.asyncio
async def test_native_otel_enforces_channel_payload_bound(theater_home, tmp_path) -> None:
    harness = compile_manifest("acme", otel_manifest(max_payload_bytes=128))
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant, credential = await prepare(daemon, harness, tmp_path)
    try:
        payload = otel_payload(participant, [otel_record(native_id="one", export_id="one")])
        assert len(payload) > 128
        assert (await post(daemon.otel_runtime.endpoint, payload, token=credential.token))[0] == 413
        source = daemon.observer._open_source(participant.id, harness.observer)
        assert source is not None
        health = source._enrichments[0].source.channel_health()
        assert health is not None and health.state.value == "degraded"
        await source.aclose()
    finally:
        await daemon.aclose()


@pytest.mark.asyncio
async def test_native_otel_overflow_decoder_failure_and_participant_cleanup(
    theater_home, tmp_path
) -> None:
    harness = compile_manifest("acme", otel_manifest(max_queue=2, max_records=3))
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant, credential = await prepare(daemon, harness, tmp_path)
    try:
        payload = otel_payload(
            participant,
            [
                otel_record(native_id="one", export_id="overflow"),
                otel_record(native_id="two", export_id="overflow"),
                otel_record(native_id="three", export_id="overflow"),
            ],
        )
        response = await post(daemon.otel_runtime.endpoint, payload, token=credential.token)
        assert response[0] == 200
        assert json.loads(response[2]) == {
            "partialSuccess": {
                "rejectedLogRecords": "1",
                "errorMessage": "native OTel inbox overflow",
            }
        }
        source = daemon.observer._open_source(participant.id, harness.observer)
        assert source is not None
        assert [fact.native_id for fact in (await source.read()).trajectory] == ["one", "two"]
        health = source._enrichments[0].source.channel_health()
        assert health is not None and health.dropped == 1
        bad = otel_payload(
            participant, [otel_record(native_id="bad", export_id="bad", mode="error")]
        )
        assert (await post(daemon.otel_runtime.endpoint, bad, token=credential.token))[0] == 200
        assert (await source._enrichments[0].source.read()).error_code == "otel_decode_failed"
        daemon.registry.mark_dead(participant.id)
        assert not credential.token_path.exists()
        assert source._enrichments[0].source.channel_health() is None
        await source.aclose()
    finally:
        await daemon.aclose()


@pytest.mark.asyncio
async def test_native_otel_recovers_after_overflow(theater_home, tmp_path) -> None:
    harness = compile_manifest("acme", otel_manifest(max_queue=1, max_records=2))
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant, credential = await prepare(daemon, harness, tmp_path)
    try:
        first = otel_payload(
            participant,
            [
                otel_record(native_id="first", export_id="first"),
                otel_record(native_id="dropped", export_id="first"),
            ],
        )
        assert (await post(daemon.otel_runtime.endpoint, first, token=credential.token))[0] == 200
        source = daemon.observer._open_source(participant.id, harness.observer)
        assert source is not None
        assert [fact.native_id for fact in (await source.read()).trajectory] == ["first"]
        second = otel_payload(participant, [otel_record(native_id="recovered", export_id="second")])
        assert (await post(daemon.otel_runtime.endpoint, second, token=credential.token))[0] == 200
        assert [fact.native_id for fact in (await source.read()).trajectory] == ["recovered"]
        assert source._enrichments[0].source.channel_health().state.value == "healthy"
        await source.aclose()
    finally:
        await daemon.aclose()


@pytest.mark.asyncio
async def test_native_otel_authentication_precedes_decoding_and_scopes_health(
    theater_home, tmp_path
) -> None:
    harness = compile_manifest("acme", otel_manifest())
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    first, first_credential = await prepare(daemon, harness, tmp_path, pid="p-otel-first")
    second, second_credential = await prepare(daemon, harness, tmp_path, pid="p-otel-second")
    first_source = daemon.observer._open_source(first.id, harness.observer)
    second_source = daemon.observer._open_source(second.id, harness.observer)
    assert isinstance(first_source, CompositeSource)
    assert isinstance(second_source, CompositeSource)
    first_otel = first_source._enrichments[0].source
    second_otel = second_source._enrichments[0].source
    try:
        first_before = first_otel.channel_health()
        second_before = second_otel.channel_health()
        forged = await post(daemon.otel_runtime.endpoint, b"{", token="forged")
        assert forged[0] == 401
        assert first_otel.channel_health() == first_before
        assert second_otel.channel_health() == second_before

        wrong_resource = otel_payload(second, [otel_record(native_id="wrong", export_id="wrong")])
        rejected = await post(
            daemon.otel_runtime.endpoint,
            wrong_resource,
            token=first_credential.token,
        )
        assert rejected[0] == 403
        assert first_otel.channel_health().state.value == "degraded"
        assert second_otel.channel_health() == second_before

        daemon.otel_runtime.activate(
            participant_id=second.id,
            harness=second.harness,
            channel=harness.observer.enrichment_manifests()[0],
            credential=replace(second_credential, token=first_credential.token),
        )
        first_after_resource = first_otel.channel_health()
        ambiguous = await post(daemon.otel_runtime.endpoint, b"{", token=first_credential.token)
        assert ambiguous[0] == 401
        assert first_otel.channel_health() == first_after_resource
        assert second_otel.channel_health() == second_before
    finally:
        await first_source.aclose()
        await second_source.aclose()
        await daemon.aclose()


@pytest.mark.asyncio
async def test_native_otel_record_keys_accept_maximum_export_identity(
    theater_home, tmp_path
) -> None:
    harness = compile_manifest("acme", otel_manifest(max_queue=3, max_records=2))
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant, credential = await prepare(daemon, harness, tmp_path)
    source = daemon.observer._open_source(participant.id, harness.observer)
    assert isinstance(source, CompositeSource)
    try:
        export_id = "x" * HARNESS_CHANNEL_IDENTIFIER_MAX_CHARS
        payload = otel_payload(
            participant,
            [
                otel_record(native_id="maximum-one", export_id=export_id),
                otel_record(native_id="maximum-two", export_id=export_id),
            ],
        )
        assert (await post(daemon.otel_runtime.endpoint, payload, token=credential.token))[0] == 200
        assert [fact.native_id for fact in (await source.read()).trajectory] == [
            "maximum-one",
            "maximum-two",
        ]
        assert (await post(daemon.otel_runtime.endpoint, payload, token=credential.token))[0] == 200
        assert (await source.read()).trajectory == ()
    finally:
        await source.aclose()
        await daemon.aclose()


@pytest.mark.asyncio
async def test_native_otel_receiver_enforces_header_count_and_size(theater_home, tmp_path) -> None:
    harness = compile_manifest("acme", otel_manifest())
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    _participant, credential = await prepare(daemon, harness, tmp_path)
    try:
        too_many = await post(
            daemon.otel_runtime.endpoint,
            b"",
            token=credential.token,
            extra_headers=tuple((f"X-Count-{index}", "x") for index in range(61)),
        )
        assert too_many[0] == 400 and json.loads(too_many[2])["code"] == 3
        too_large = await post(
            daemon.otel_runtime.endpoint,
            b"",
            token=credential.token,
            extra_headers=tuple((f"X-Size-{index}", "x" * 140) for index in range(60)),
        )
        assert too_large[0] == 400 and json.loads(too_large[2])["code"] == 3
    finally:
        await daemon.aclose()


@pytest.mark.asyncio
async def test_native_otel_parse_capacity_is_bounded(monkeypatch) -> None:
    gate = threading.Event()
    runtime = NativeOtelRuntime(lambda _participant, otel_channel: None)

    def blocked(_body: bytes) -> tuple[()]:
        gate.wait(timeout=1.0)
        return ()

    monkeypatch.setattr(otel_runtime, "decode_otlp_json", blocked)
    tasks = [
        asyncio.create_task(runtime._decode(OtelProtocol.OTLP_HTTP_JSON, b"{}"))
        for _ in range(HARNESS_OTEL_PARSE_MAX_IN_FLIGHT)
    ]
    try:
        for _ in range(100):
            if runtime._parse_in_flight == HARNESS_OTEL_PARSE_MAX_IN_FLIGHT:
                break
            await asyncio.sleep(0)
        assert runtime._parse_in_flight == HARNESS_OTEL_PARSE_MAX_IN_FLIGHT
        with pytest.raises(OtelHttpError) as overloaded:
            await runtime._decode(OtelProtocol.OTLP_HTTP_JSON, b"{}")
        assert overloaded.value.status == 429
    finally:
        gate.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.aclose()


@pytest.mark.asyncio
async def test_native_otel_receiver_bounds_timeout_overload_and_shutdown() -> None:
    async def accepted(_path, _headers, _body) -> OtelHttpResponse:
        return OtelHttpResponse()

    receiver = NativeOtelReceiver(accepted, max_concurrent=1, request_timeout=0.02, backlog=1)
    await receiver.start()
    host, port = endpoint_parts(receiver.endpoint)
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(b"POST /v1/logs HTTP/1.1\r\n")
    await writer.drain()
    for _ in range(20):
        if receiver._in_flight == 1:
            break
        await asyncio.sleep(0)
    try:
        assert receiver._in_flight == 1
        overloaded = await post(receiver.endpoint, b"{}", token="unused")
        assert overloaded[0] == 429
        assert json.loads(overloaded[2])["code"] == 8
        timeout_status = (await reader.readline()).decode().split()
        assert timeout_status[1] == "408"
        assert b'"code": 4' in await reader.read()
    finally:
        writer.close()
        await writer.wait_closed()
        await receiver.aclose()

    open_receiver = NativeOtelReceiver(accepted, request_timeout=1.0)
    await open_receiver.start()
    host, port = endpoint_parts(open_receiver.endpoint)
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(b"POST /v1/logs HTTP/1.1\r\n")
    await writer.drain()
    for _ in range(20):
        if open_receiver._handlers:
            break
        await asyncio.sleep(0)
    try:
        assert open_receiver._handlers
        await open_receiver.aclose()
        assert open_receiver._handlers == set()
        await reader.read()
        assert reader.at_eof()
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_native_otel_receiver_returns_retryable_unavailable_response() -> None:
    async def unavailable(_path, _headers, _body) -> OtelHttpResponse:
        raise OtelHttpError("native OTel receiver is unavailable", status=503)

    receiver = NativeOtelReceiver(unavailable)
    await receiver.start()
    try:
        response = await post(receiver.endpoint, b"{}", token="unused")
        assert response[0] == 503
        assert response[1]["content-type"] == "application/json"
        assert json.loads(response[2])["code"] == 14
    finally:
        await receiver.aclose()


def test_native_otel_receiver_refuses_non_loopback() -> None:
    with pytest.raises(ValueError, match="loopback"):
        NativeOtelRuntime(lambda _participant, otel_channel: None, bind_host="0.0.0.0")
