"""Focused generic native OTel channel tests."""

from __future__ import annotations

import asyncio
import json
import stat
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from theater.constants.daemon import CHANNEL_OTEL_RECEIVER_PORT_META_KEY
from theater.constants.harness import (
    HARNESS_CHANNEL_IDENTIFIER_MAX_CHARS,
    HARNESS_OTEL_PARSE_MAX_IN_FLIGHT,
)
from theater.daemon.server import Daemon
from theater.daemon.spawning.planning import (
    install_hook_plan,
    install_otel_plan,
    record_launch_identity,
    validate_receipt_plan,
    write_plan_files,
)
from theater.harness.builtin.plugins.claude.manifest import MANIFEST as CLAUDE_MANIFEST
from theater.harness.builtin.plugins.codex.manifest import MANIFEST as CODEX_MANIFEST
from theater.harness.builtin.plugins.opencode.manifest import MANIFEST as OPENCODE_MANIFEST
from theater.harness.builtin.plugins.vibe.manifest import MANIFEST as VIBE_MANIFEST
from theater.harness.channels import CompositeSource
from theater.harness.channels.otel import NativeOtelRuntime
from theater.harness.channels.otel import bounds as otel_bounds
from theater.harness.channels.otel import runtime as otel_runtime
from theater.harness.channels.otel.callbacks import OtelCallbackRunner
from theater.harness.channels.otel.inbox import OtelDelivery
from theater.harness.channels.otel.receiver import (
    NativeOtelReceiver,
    OtelHttpError,
    OtelHttpResponse,
)
from theater.harness.contracts.callbacks import (
    HookInstallOverlay,
    LaunchContext,
    OtelCorrelationContext,
    OtelDecodeContext,
    OtelInstallContext,
    OtelInstallOverlay,
    ScreenContext,
)
from theater.harness.contracts.channels import (
    ChannelBounds,
    ChannelCapability,
    ChannelDeclaration,
    ChannelFact,
    ChannelKind,
    HookBinding,
    OtelBinding,
    OtelBounds,
    OtelCorrelation,
    OtelProtocol,
    OtelSignal,
    SignalKind,
    SignalOwnership,
)
from theater.harness.contracts.launch import ChannelCredential, LaunchPlan
from theater.harness.contracts.manifest import (
    MANIFEST_API_VERSION,
    HarnessManifest,
    HookChannelManifest,
    LaunchManifest,
    ObservationManifest,
    OtelChannelManifest,
    ScreenManifest,
    SourceManifest,
)
from theater.harness.contracts.observation import ScreenConfidence, ScreenKind, ScreenReading
from theater.harness.contracts.source import Batch, Source
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.harness.loading.models import LoadedPlugin
from theater.harness.manifests.compiler import compile_manifest
from theater.harness.manifests.validation import ManifestValidationError
from theater.harness.registry.diagnostics import project_plugin
from theater.models import BadRequest
from theater.trajectory.enums import TrajectoryKind


class _Primary(Source):
    async def read(self) -> Batch:
        return Batch()


def _primary(_context) -> Source:
    return _Primary()


def _screen(_context: ScreenContext) -> ScreenReading:
    return ScreenReading(ScreenKind.PROMPT, ScreenConfidence.HIGH)


def _plan(context: LaunchContext) -> LaunchPlan:
    return LaunchPlan(argv=["acme", context.participant_id])


def _correlate(context: OtelCorrelationContext) -> str:
    value = context.record.attributes.get("fixture.native")
    if not isinstance(value, str):
        raise TypeError("native identity is absent")
    return value


def _decode(context: OtelDecodeContext) -> tuple[ChannelFact, ...]:
    if context.record.attributes.get("fixture.mode") == "error":
        raise RuntimeError("decoder failed")
    return (
        ChannelFact(
            SignalKind.TOOL,
            TrajectoryFact(
                kind=TrajectoryKind.TOOL_CALL,
                native_id=context.native_id,
                revision=int(context.record.attributes.get("fixture.revision", 0)),
            ),
        ),
    )


def _install(context: OtelInstallContext) -> OtelInstallOverlay:
    assert not hasattr(context, "token")
    config = context.token_file.with_name("native-otel.json")
    return OtelInstallOverlay(
        env={"ACME_NATIVE_OTEL_CONFIG": str(config)},
        files={config: f"{context.endpoint}:{context.auth_header}:{context.token_file}"},
        credential_header_env="ACME_NATIVE_OTEL_HEADERS",
    )


def _channel(
    *,
    max_queue: int = 3,
    max_records: int = 3,
    max_payload_bytes: int = 4096,
    protocol: OtelProtocol = OtelProtocol.OTLP_HTTP_JSON,
    decoder=_decode,
) -> OtelChannelManifest:
    return OtelChannelManifest(
        declaration=ChannelDeclaration(
            id="native-otel",
            kind=ChannelKind.OTEL,
            capabilities=(ChannelCapability(SignalKind.TOOL, SignalOwnership.ENRICHMENT),),
            bounds=ChannelBounds(max_queue=max_queue, max_payload_bytes=max_payload_bytes),
        ),
        protocol=protocol,
        bounds=OtelBounds(
            max_records=max_records,
            max_attributes=8,
            max_value_depth=4,
            max_text_bytes=256,
        ),
        correlation=OtelCorrelation(
            auth_header="x-fixture-token",
            participant_attribute="fixture.participant",
            harness_attribute="fixture.harness",
            channel_attribute="fixture.channel",
            binding_attribute="fixture.signal",
            delivery_id_attribute="fixture.export",
        ),
        bindings=(
            OtelBinding(
                name="tool.finished",
                signal=OtelSignal.LOGS,
                signals=(SignalKind.TOOL,),
                decoder=decoder,
                correlation=_correlate,
            ),
        ),
        installer=_install,
    )


def _manifest(*, primary: bool = True, **kwargs) -> HarnessManifest:
    return HarnessManifest(
        api_version=MANIFEST_API_VERSION,
        binary="acme",
        icon="A",
        launch=LaunchManifest(planner=_plan, approvals=("manual",)),
        observation=ObservationManifest(
            primary=(
                SourceManifest(
                    factory=_primary,
                    channel=ChannelDeclaration(id="primary", kind=ChannelKind.TRANSCRIPT),
                )
                if primary
                else None
            ),
            screen=ScreenManifest(classifier=_screen),
            enrichments=(_channel(**kwargs),),
        ),
    )


def test_manifest_projection_includes_hook_and_otel_native_bindings() -> None:
    manifest = _manifest()
    hook = _hook_channel()
    manifest = replace(
        manifest,
        observation=replace(
            manifest.observation,
            enrichments=(hook, *manifest.observation.enrichments),
        ),
    )
    compiled = compile_manifest("acme", manifest)
    row = project_plugin(
        {
            "name": "acme",
            "icon": "A",
            "binary": "acme",
            "installed": True,
            "path": "/usr/bin/acme",
            "source": "local",
            "error": None,
        },
        LoadedPlugin(
            path=Path("/tmp/acme"),
            source="local",
            name="acme",
            harness=compiled,
            manifest=manifest,
        ),
        None,
    )

    channels = {channel["id"]: channel for channel in row["channels"]}
    assert channels["native-hook"]["bindings"] == [
        {"event": "tool.finished", "delivery": "best_effort", "signals": ["lifecycle"]}
    ]
    assert channels["native-otel"]["protocol"] == "otlp_http_json"
    assert channels["native-otel"]["bindings"] == [
        {"name": "tool.finished", "signal": "logs", "signals": ["tool"]}
    ]


def _value(key: str, value: object) -> dict[str, object]:
    if isinstance(value, str):
        typed = {"stringValue": value}
    elif type(value) is int:
        typed = {"intValue": str(value)}
    else:
        raise TypeError("fixture supports strings and integers")
    return {"key": key, "value": typed}


def _record(
    *,
    native_id: str,
    export_id: str,
    revision: int = 0,
    mode: str | None = None,
) -> dict[str, object]:
    attributes = [
        _value("fixture.signal", "tool.finished"),
        _value("fixture.export", export_id),
        _value("fixture.native", native_id),
        _value("fixture.revision", revision),
    ]
    if mode is not None:
        attributes.append(_value("fixture.mode", mode))
    return {
        "timeUnixNano": "42",
        "observedTimeUnixNano": "43",
        "traceId": "trace",
        "spanId": "span",
        "severityNumber": 9,
        "severityText": "INFO",
        "body": {"stringValue": "fixture"},
        "attributes": attributes,
    }


def _payload(participant, records: list[dict[str, object]], *, harness: str | None = None) -> bytes:
    return json.dumps(
        {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            _value("fixture.participant", participant.id),
                            _value("fixture.harness", harness or participant.harness),
                            _value("fixture.channel", "native-otel"),
                        ]
                    },
                    "scopeLogs": [{"logRecords": records}],
                }
            ]
        }
    ).encode()


def _hook_channel() -> HookChannelManifest:
    return HookChannelManifest(
        declaration=ChannelDeclaration(
            id="native-hook",
            kind=ChannelKind.HOOK,
            capabilities=(ChannelCapability(SignalKind.LIFECYCLE, SignalOwnership.ENRICHMENT),),
        ),
        bindings=(
            HookBinding(
                event="tool.finished",
                signals=(SignalKind.LIFECYCLE,),
                decoder=lambda _context: (),
                correlation=lambda _context: "hook",
            ),
        ),
        installer=lambda _context: HookInstallOverlay(env={"ACME_NATIVE_HOOK": "enabled"}),
    )


def _endpoint_parts(endpoint: str) -> tuple[str, int]:
    host_port = endpoint.removeprefix("http://")
    host, port = host_port.rsplit(":", 1)
    return host, int(port)


async def _post(
    endpoint: str,
    body: bytes,
    *,
    token: str | None = None,
    path: str = "/v1/logs",
    content_type: str = "application/json",
    method: str = "POST",
    extra_headers: tuple[tuple[str, str], ...] = (),
) -> tuple[int, dict[str, str], bytes]:
    host, port = _endpoint_parts(endpoint)
    reader, writer = await asyncio.open_connection(host, port)
    token_header = "" if token is None else f"X-Fixture-Token: {token}\r\n"
    additional = "".join(f"{key}: {value}\r\n" for key, value in extra_headers)
    request = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"{token_header}{additional}"
        "\r\n"
    )
    writer.write(request.encode("ascii") + body)
    await writer.drain()
    status = (await reader.readline()).decode().split()
    headers: dict[str, str] = {}
    while line := await reader.readline():
        if line == b"\r\n":
            break
        key, value = line.decode().split(":", 1)
        headers[key.lower()] = value.strip()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    return int(status[1]), headers, response


async def _malformed_status(endpoint: str) -> int:
    host, port = _endpoint_parts(endpoint)
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(b"not an HTTP request\r\n\r\n")
    await writer.drain()
    status = (await reader.readline()).decode().split()
    writer.close()
    await writer.wait_closed()
    return int(status[1])


async def _raw_status(endpoint: str, request: bytes) -> int:
    host, port = _endpoint_parts(endpoint)
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(request)
    await writer.drain()
    status = (await reader.readline()).decode().split()
    writer.close()
    await writer.wait_closed()
    return int(status[1])


async def _prepare(daemon: Daemon, harness, tmp_path, *, pid: str = "p-otel"):
    participant = daemon.registry.create_spawned(harness="acme", cwd=str(tmp_path), pid=pid)
    plan = install_otel_plan(
        LaunchPlan(argv=["acme"]), participant, harness.observer, daemon.otel_runtime
    )
    record_launch_identity(
        participant,
        plan,
        daemon.registry,
        runtime=daemon.otel_runtime,
        observer=harness.observer,
    )
    write_plan_files(plan)
    credential = plan.channel_credentials[0]
    assert credential.kind is ChannelKind.OTEL
    assert plan.env["ACME_NATIVE_OTEL_HEADERS"] == f"x-fixture-token={credential.token}"
    assert credential.token not in "\n".join(plan.files.values())
    return participant, credential


@pytest.mark.asyncio
async def test_channel_credentials_are_kind_scoped(theater_home, tmp_path) -> None:
    daemon = Daemon(harnesses={})
    await daemon.start()
    participant = daemon.registry.create_spawned(harness="acme", cwd=str(tmp_path), pid="scoped")
    try:
        daemon.store.set_channel_credential(
            participant.id,
            harness="acme",
            kind=ChannelKind.HOOK,
            channel_id="native",
            token="hook-token",
            token_path=str(tmp_path / "hook.token"),
        )
        daemon.store.set_channel_credential(
            participant.id,
            harness="acme",
            kind=ChannelKind.OTEL,
            channel_id="native",
            token="otel-token",
            token_path=str(tmp_path / "otel.token"),
        )
        hook = daemon.store.get_channel_credential(participant.id, ChannelKind.HOOK, "native")
        otel = daemon.store.get_channel_credential(participant.id, ChannelKind.OTEL, "native")
        assert hook is not None and hook.token == "hook-token" and hook.kind is ChannelKind.HOOK
        assert otel is not None and otel.token == "otel-token" and otel.kind is ChannelKind.OTEL
    finally:
        await daemon.aclose()


@pytest.mark.asyncio
async def test_native_otel_loopback_ingests_multiple_records_and_retries(
    theater_home, tmp_path
) -> None:
    harness = compile_manifest("acme", _manifest(max_queue=3, max_records=3))
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant, credential = await _prepare(daemon, harness, tmp_path)
    try:
        assert stat.S_IMODE(credential.token_path.stat().st_mode) == 0o600
        assert (
            credential.token not in credential.token_path.with_name("native-otel.json").read_text()
        )
        payload = _payload(
            participant,
            [
                _record(native_id="one", export_id="export-a"),
                _record(native_id="two", export_id="export-a"),
            ],
        )
        status, headers, body = await _post(
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
        retry = await _post(daemon.otel_runtime.endpoint, payload, token=credential.token)
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
        "acme", _manifest(protocol=OtelProtocol.OTLP_HTTP_PROTOBUF, max_queue=1, max_records=2)
    )
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant, credential = await _prepare(daemon, harness, tmp_path)
    try:
        request = ExportLogsServiceRequest()
        records = [_record(native_id="protobuf", export_id="proto")]
        records[0]["traceId"] = "dHJhY2U="
        records[0]["spanId"] = "c3Bhbg=="
        ParseDict(
            json.loads(_payload(participant, records)),
            request,
        )
        status, headers, body = await _post(
            daemon.otel_runtime.endpoint,
            request.SerializeToString(),
            token=credential.token,
            content_type="application/x-protobuf",
        )
        assert (status, headers["content-type"], body) == (200, "application/x-protobuf", b"")
        rejected = await _post(
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
            _record(native_id="overflow-one", export_id="protobuf-overflow"),
            _record(native_id="overflow-two", export_id="protobuf-overflow"),
        ]
        for record in overflow_records:
            record["traceId"] = "dHJhY2U="
            record["spanId"] = "c3Bhbg=="
        ParseDict(
            json.loads(
                _payload(
                    participant,
                    overflow_records,
                )
            ),
            overflow,
        )
        partial = await _post(
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
            json.loads(_payload(participant, records, harness="other")),
            wrong_resource,
        )
        forbidden = await _post(
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
    harness = compile_manifest("acme", _manifest(max_queue=2, max_records=2))
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant, credential = await _prepare(daemon, harness, tmp_path)
    try:
        payload = _payload(participant, [_record(native_id="one", export_id="one")])
        assert await _malformed_status(daemon.otel_runtime.endpoint) == 400
        missing = await _post(daemon.otel_runtime.endpoint, payload)
        assert missing[0] == 401
        assert json.loads(missing[2]) == {
            "code": 16,
            "message": "native OTel credential is invalid",
        }
        rejected = await _post(daemon.otel_runtime.endpoint, payload, token="forged")
        assert rejected[0] == 401
        wrong_harness = _payload(
            participant, [_record(native_id="one", export_id="two")], harness="other"
        )
        assert (await _post(daemon.otel_runtime.endpoint, wrong_harness, token=credential.token))[
            0
        ] == 403
        wrong_path = await _post(
            daemon.otel_runtime.endpoint, payload, token=credential.token, path="/other"
        )
        assert wrong_path[0] == 404 and json.loads(wrong_path[2])["code"] == 5
        wrong_method = await _post(
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
        typed = await _post(
            daemon.otel_runtime.endpoint,
            payload,
            token=credential.token,
            content_type="text/plain",
        )
        assert typed[0] == 415 and typed[1]["content-type"] == "application/json"
        overflow = _payload(
            participant,
            [
                _record(native_id="one", export_id="batch"),
                _record(native_id="two", export_id="batch"),
                _record(native_id="three", export_id="batch"),
            ],
        )
        assert (await _post(daemon.otel_runtime.endpoint, overflow, token=credential.token))[
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
    harness = compile_manifest("acme", _manifest(max_payload_bytes=128))
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant, credential = await _prepare(daemon, harness, tmp_path)
    try:
        payload = _payload(participant, [_record(native_id="one", export_id="one")])
        assert len(payload) > 128
        assert (await _post(daemon.otel_runtime.endpoint, payload, token=credential.token))[
            0
        ] == 413
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
    harness = compile_manifest("acme", _manifest(max_queue=2, max_records=3))
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant, credential = await _prepare(daemon, harness, tmp_path)
    try:
        payload = _payload(
            participant,
            [
                _record(native_id="one", export_id="overflow"),
                _record(native_id="two", export_id="overflow"),
                _record(native_id="three", export_id="overflow"),
            ],
        )
        response = await _post(daemon.otel_runtime.endpoint, payload, token=credential.token)
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
        bad = _payload(participant, [_record(native_id="bad", export_id="bad", mode="error")])
        assert (await _post(daemon.otel_runtime.endpoint, bad, token=credential.token))[0] == 200
        assert (await source._enrichments[0].source.read()).error_code == "otel_decode_failed"
        daemon.registry.mark_dead(participant.id)
        assert not credential.token_path.exists()
        assert source._enrichments[0].source.channel_health() is None
        await source.aclose()
    finally:
        await daemon.aclose()


@pytest.mark.asyncio
async def test_native_otel_recovers_after_overflow(theater_home, tmp_path) -> None:
    harness = compile_manifest("acme", _manifest(max_queue=1, max_records=2))
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant, credential = await _prepare(daemon, harness, tmp_path)
    try:
        first = _payload(
            participant,
            [
                _record(native_id="first", export_id="first"),
                _record(native_id="dropped", export_id="first"),
            ],
        )
        assert (await _post(daemon.otel_runtime.endpoint, first, token=credential.token))[0] == 200
        source = daemon.observer._open_source(participant.id, harness.observer)
        assert source is not None
        assert [fact.native_id for fact in (await source.read()).trajectory] == ["first"]
        second = _payload(participant, [_record(native_id="recovered", export_id="second")])
        assert (await _post(daemon.otel_runtime.endpoint, second, token=credential.token))[0] == 200
        assert [fact.native_id for fact in (await source.read()).trajectory] == ["recovered"]
        assert source._enrichments[0].source.channel_health().state.value == "healthy"
        await source.aclose()
    finally:
        await daemon.aclose()


@pytest.mark.asyncio
async def test_native_otel_restores_credentials_after_restart(theater_home, tmp_path) -> None:
    harness = compile_manifest("acme", _manifest())
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant, credential = await _prepare(daemon, harness, tmp_path)
    endpoint = daemon.otel_runtime.endpoint
    config = credential.token_path.with_name("native-otel.json")
    assert endpoint in config.read_text()
    await daemon.aclose()

    restarted = Daemon(harnesses={"acme": harness})
    await restarted.start()
    try:
        assert restarted.otel_runtime.endpoint == endpoint
        assert endpoint in config.read_text()
        payload = _payload(participant, [_record(native_id="after", export_id="after")])
        assert (await _post(endpoint, payload, token=credential.token))[0] == 200
        source = restarted.observer._open_source(participant.id, harness.observer)
        assert source is not None
        assert [fact.native_id for fact in (await source.read()).trajectory] == ["after"]
        await source.aclose()
    finally:
        await restarted.aclose()


@pytest.mark.asyncio
async def test_native_otel_receiver_replaces_an_occupied_persisted_port(
    theater_home, tmp_path
) -> None:
    harness = compile_manifest("acme", _manifest())
    blocker = await asyncio.start_server(lambda _reader, _writer: None, host="127.0.0.1", port=0)
    port = blocker.sockets[0].getsockname()[1]
    daemon = Daemon(harnesses={"acme": harness})
    daemon.store.set_meta(CHANNEL_OTEL_RECEIVER_PORT_META_KEY, str(port))
    await daemon.start()
    participant = daemon.registry.create_spawned(harness="acme", cwd=str(tmp_path), pid="blocked")
    try:
        assert daemon.otel_runtime.available is True
        replacement_port = _endpoint_parts(daemon.otel_runtime.endpoint)[1]
        assert replacement_port != port
        assert daemon.store.get_meta(CHANNEL_OTEL_RECEIVER_PORT_META_KEY) == str(replacement_port)
        plan = install_otel_plan(
            LaunchPlan(argv=["acme"]), participant, harness.observer, daemon.otel_runtime
        )
        assert len(plan.channel_credentials) == 1
    finally:
        blocker.close()
        await blocker.wait_closed()
        await daemon.aclose()


@pytest.mark.asyncio
async def test_native_otel_receiver_replaces_an_invalid_persisted_port(theater_home) -> None:
    harness = compile_manifest("acme", _manifest())
    daemon = Daemon(harnesses={"acme": harness})
    daemon.store.set_meta(CHANNEL_OTEL_RECEIVER_PORT_META_KEY, "invalid")
    await daemon.start()
    try:
        port = _endpoint_parts(daemon.otel_runtime.endpoint)[1]
        assert daemon.otel_runtime.available is True
        assert daemon.store.get_meta(CHANNEL_OTEL_RECEIVER_PORT_META_KEY) == str(port)
    finally:
        await daemon.aclose()


@pytest.mark.asyncio
async def test_native_otel_installer_preserves_inherited_exporter_environment(
    theater_home, tmp_path, monkeypatch
) -> None:
    harness = compile_manifest("acme", _manifest())
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant = daemon.registry.create_spawned(harness="acme", cwd=str(tmp_path), pid="env")
    try:
        monkeypatch.setenv("ACME_NATIVE_OTEL_CONFIG", "user-exporter")
        with pytest.raises(BadRequest) as config_collision:
            install_otel_plan(
                LaunchPlan(argv=["acme"]), participant, harness.observer, daemon.otel_runtime
            )
        assert "ACME_NATIVE_OTEL_CONFIG" in str(config_collision.value)
        assert "user-exporter" not in str(config_collision.value)

        monkeypatch.delenv("ACME_NATIVE_OTEL_CONFIG")
        monkeypatch.setenv("ACME_NATIVE_OTEL_HEADERS", "user-headers")
        with pytest.raises(BadRequest) as header_collision:
            install_otel_plan(
                LaunchPlan(argv=["acme"]), participant, harness.observer, daemon.otel_runtime
            )
        assert "ACME_NATIVE_OTEL_HEADERS" in str(header_collision.value)
        assert "user-headers" not in str(header_collision.value)
    finally:
        await daemon.aclose()


@pytest.mark.asyncio
async def test_native_otel_authentication_precedes_decoding_and_scopes_health(
    theater_home, tmp_path
) -> None:
    harness = compile_manifest("acme", _manifest())
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    first, first_credential = await _prepare(daemon, harness, tmp_path, pid="p-otel-first")
    second, second_credential = await _prepare(daemon, harness, tmp_path, pid="p-otel-second")
    first_source = daemon.observer._open_source(first.id, harness.observer)
    second_source = daemon.observer._open_source(second.id, harness.observer)
    assert isinstance(first_source, CompositeSource)
    assert isinstance(second_source, CompositeSource)
    first_otel = first_source._enrichments[0].source
    second_otel = second_source._enrichments[0].source
    try:
        first_before = first_otel.channel_health()
        second_before = second_otel.channel_health()
        forged = await _post(daemon.otel_runtime.endpoint, b"{", token="forged")
        assert forged[0] == 401
        assert first_otel.channel_health() == first_before
        assert second_otel.channel_health() == second_before

        wrong_resource = _payload(second, [_record(native_id="wrong", export_id="wrong")])
        rejected = await _post(
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
        ambiguous = await _post(daemon.otel_runtime.endpoint, b"{", token=first_credential.token)
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
    harness = compile_manifest("acme", _manifest(max_queue=3, max_records=2))
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant, credential = await _prepare(daemon, harness, tmp_path)
    source = daemon.observer._open_source(participant.id, harness.observer)
    assert isinstance(source, CompositeSource)
    try:
        export_id = "x" * HARNESS_CHANNEL_IDENTIFIER_MAX_CHARS
        payload = _payload(
            participant,
            [
                _record(native_id="maximum-one", export_id=export_id),
                _record(native_id="maximum-two", export_id=export_id),
            ],
        )
        assert (await _post(daemon.otel_runtime.endpoint, payload, token=credential.token))[
            0
        ] == 200
        assert [fact.native_id for fact in (await source.read()).trajectory] == [
            "maximum-one",
            "maximum-two",
        ]
        assert (await _post(daemon.otel_runtime.endpoint, payload, token=credential.token))[
            0
        ] == 200
        assert (await source.read()).trajectory == ()
    finally:
        await source.aclose()
        await daemon.aclose()


@pytest.mark.asyncio
async def test_native_otel_missing_optional_decoder_degrades_only_channel(
    theater_home,
    tmp_path,
    monkeypatch,
) -> None:
    harness = compile_manifest("acme", _manifest(protocol=OtelProtocol.OTLP_HTTP_PROTOBUF))
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant, credential = await _prepare(daemon, harness, tmp_path)

    def missing(_name: str):
        raise ImportError("not installed")

    monkeypatch.setattr(otel_bounds.importlib, "import_module", missing)
    try:
        rejected = await _post(
            daemon.otel_runtime.endpoint,
            b"not-a-protobuf-request",
            token=credential.token,
            content_type="application/x-protobuf",
        )
        assert rejected[0] == 400
        source = daemon.observer._open_source(participant.id, harness.observer)
        assert source is not None
        assert (await source.read()).error_code is None
        health = source._enrichments[0].source.channel_health()
        assert health is not None and health.state.value == "degraded"
        assert "optional dependency" in health.diagnostics[-1]
        await source.aclose()
    finally:
        await daemon.aclose()


def test_native_otel_manifest_validation_and_shipped_unavailability() -> None:
    manifest = _manifest()
    channel = manifest.observation.otel_channels[0]
    invalid = replace(channel, declaration=replace(channel.declaration, kind=ChannelKind.HOOK))
    with pytest.raises(ManifestValidationError) as raised:
        compile_manifest(
            "acme",
            replace(
                manifest,
                observation=replace(manifest.observation, enrichments=(invalid,)),
            ),
        )
    assert raised.value.path == "observation.enrichments[0].declaration.kind"
    unavailable = replace(channel, unavailable_reason="not safe")
    with pytest.raises(ManifestValidationError) as raised:
        compile_manifest(
            "acme",
            replace(
                manifest,
                observation=replace(manifest.observation, enrichments=(unavailable,)),
            ),
        )
    assert raised.value.path == "observation.enrichments[0].bindings"
    binding = channel.bindings[0]
    invalid_cases = (
        (
            replace(channel, bindings=(binding, binding)),
            "observation.enrichments[0].bindings[1].name",
        ),
        (
            replace(channel, bindings=(replace(binding, signals=(SignalKind.USAGE,)),)),
            "observation.enrichments[0].bindings[0].signals[0]",
        ),
        (
            replace(channel, bounds=replace(channel.bounds, max_records=0)),
            "observation.enrichments[0].bounds.max_records",
        ),
        (
            replace(channel, installer=None),
            "observation.enrichments[0].installer",
        ),
        (
            replace(
                channel,
                correlation=replace(channel.correlation, auth_header="not a header"),
            ),
            "observation.enrichments[0].correlation.auth_header",
        ),
        (
            replace(
                channel,
                correlation=replace(channel.correlation, auth_header="content-type"),
            ),
            "observation.enrichments[0].correlation.auth_header",
        ),
        (
            replace(channel, bindings=(replace(binding, decoder=object()),)),
            "observation.enrichments[0].bindings[0].decoder",
        ),
    )
    for invalid_channel, path in invalid_cases:
        with pytest.raises(ManifestValidationError) as raised:
            compile_manifest(
                "acme",
                replace(
                    manifest,
                    observation=replace(manifest.observation, enrichments=(invalid_channel,)),
                ),
            )
        assert raised.value.path == path

    expected = {
        "claude": "emitted schema/fan-out/launch correlation unverified.",
        "codex": "endpoint choice replaces exporter; no safe fan-out.",
        "opencode": "one endpoint, no safe fan-out or stable join.",
        "vibe": "installs one global provider/exporter; exporter theft.",
    }
    for name, built in (
        ("claude", CLAUDE_MANIFEST),
        ("codex", CODEX_MANIFEST),
        ("opencode", OPENCODE_MANIFEST),
        ("vibe", VIBE_MANIFEST),
    ):
        compiled = compile_manifest(name, built)
        channels = compiled.observer.enrichment_manifests()
        native = next(channel for channel in channels if isinstance(channel, OtelChannelManifest))
        assert native.declaration.id == "native-otel"
        assert native.declaration.kind is ChannelKind.OTEL
        assert native.declaration.capabilities == ()
        assert native.bindings == () and native.installer is None
        assert native.unavailable_reason == expected[name]


@pytest.mark.asyncio
async def test_native_otel_disabled_keeps_primary_source_exact(theater_home, tmp_path) -> None:
    unavailable = OtelChannelManifest(
        declaration=ChannelDeclaration(id="native-otel", kind=ChannelKind.OTEL),
        unavailable_reason="not safe",
    )
    manifest = _manifest()
    harness = compile_manifest(
        "acme",
        replace(
            manifest,
            observation=replace(manifest.observation, enrichments=(unavailable,)),
        ),
    )
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant = daemon.registry.create_spawned(harness="acme", cwd=str(tmp_path), pid="disabled")
    try:
        source = daemon.observer._open_source(participant.id, harness.observer)
        assert isinstance(source, _Primary)
        assert await source.read() == Batch()
        await source.aclose()
    finally:
        await daemon.aclose()


@pytest.mark.asyncio
async def test_native_otel_only_source_emits_facts_without_identity_state(
    theater_home, tmp_path
) -> None:
    harness = compile_manifest("acme", _manifest(primary=False))
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant, credential = await _prepare(daemon, harness, tmp_path)
    source = daemon.observer._open_source(participant.id, harness.observer)
    assert isinstance(source, CompositeSource)
    assert source._primary is None
    try:
        payload = _payload(participant, [_record(native_id="otel-only", export_id="otel-only")])
        assert (await _post(daemon.otel_runtime.endpoint, payload, token=credential.token))[
            0
        ] == 200
        batch = await source.read()
        assert [fact.native_id for fact in batch.trajectory] == ["otel-only"]
        assert batch.progressed is False
        stored = daemon.store.get_participant(participant.id)
        assert stored is not None
        assert (
            stored.session_id,
            stored.session_correlation,
            stored.transcript_location,
            stored.transcript_domain,
        ) == (None, None, None, None)
    finally:
        await source.aclose()
        await daemon.aclose()


def test_native_otel_decoder_enforces_text_and_nesting_bounds() -> None:
    participant = type("P", (), {"id": "p", "harness": "a"})()
    text_payload = json.loads(_payload(participant, [_record(native_id="one", export_id="one")]))
    text_payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["body"] = {
        "stringValue": "x" * 21
    }
    with pytest.raises(otel_bounds.OtelIngressError, match="text exceeds"):
        otel_bounds.decode_otlp_json(
            json.dumps(text_payload).encode(),
            bounds=OtelBounds(
                max_records=4,
                max_attributes=8,
                max_value_depth=4,
                max_text_bytes=20,
            ),
        )

    integer_payload = json.loads(_payload(participant, [_record(native_id="one", export_id="one")]))
    integer_payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["attributes"].append(
        {"key": "fixture.integer", "value": {"intValue": str(1 << 63)}}
    )
    with pytest.raises(otel_bounds.OtelIngressError, match="integer value is out of range"):
        otel_bounds.decode_otlp_json(json.dumps(integer_payload).encode())

    nested_payload = json.loads(_payload(participant, [_record(native_id="one", export_id="one")]))
    nested_payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["body"] = {
        "arrayValue": {
            "values": [
                {
                    "arrayValue": {
                        "values": [
                            {"stringValue": "nested"},
                        ]
                    }
                }
            ]
        }
    }
    with pytest.raises(otel_bounds.OtelIngressError, match="nesting depth"):
        otel_bounds.decode_otlp_json(
            json.dumps(nested_payload).encode(),
            bounds=OtelBounds(
                max_records=4,
                max_attributes=8,
                max_value_depth=1,
                max_text_bytes=256,
            ),
        )


@pytest.mark.asyncio
async def test_native_otel_receiver_enforces_header_count_and_size(theater_home, tmp_path) -> None:
    harness = compile_manifest("acme", _manifest())
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    _participant, credential = await _prepare(daemon, harness, tmp_path)
    try:
        too_many = await _post(
            daemon.otel_runtime.endpoint,
            b"",
            token=credential.token,
            extra_headers=tuple((f"X-Count-{index}", "x") for index in range(61)),
        )
        assert too_many[0] == 400 and json.loads(too_many[2])["code"] == 3
        too_large = await _post(
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
    runtime = NativeOtelRuntime(lambda _participant, _channel: None)

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
    host, port = _endpoint_parts(receiver.endpoint)
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(b"POST /v1/logs HTTP/1.1\r\n")
    await writer.drain()
    for _ in range(20):
        if receiver._in_flight == 1:
            break
        await asyncio.sleep(0)
    try:
        assert receiver._in_flight == 1
        overloaded = await _post(receiver.endpoint, b"{}", token="unused")
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
    host, port = _endpoint_parts(open_receiver.endpoint)
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
        response = await _post(receiver.endpoint, b"{}", token="unused")
        assert response[0] == 503
        assert response[1]["content-type"] == "application/json"
        assert json.loads(response[2])["code"] == 14
    finally:
        await receiver.aclose()


@pytest.mark.asyncio
async def test_channel_credential_preflight_precedes_installation_and_appends_channels(
    theater_home, tmp_path
) -> None:
    manifest = _manifest()
    harness = compile_manifest(
        "acme",
        replace(
            manifest,
            observation=replace(
                manifest.observation,
                enrichments=(_hook_channel(), *manifest.observation.enrichments),
            ),
        ),
    )
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant = daemon.registry.create_spawned(harness="acme", cwd=str(tmp_path), pid="preflight")
    supplied = LaunchPlan(
        argv=["acme"],
        channel_credentials=(
            ChannelCredential(
                kind=ChannelKind.HOOK,
                channel_id="plugin-supplied",
                token="plugin-secret",
                token_path=tmp_path / "plugin.token",
            ),
        ),
    )
    try:
        with pytest.raises(BadRequest, match="channel_credentials"):
            validate_receipt_plan(supplied, participant)

        plan = install_otel_plan(
            install_hook_plan(LaunchPlan(argv=["acme"]), participant, harness.observer),
            participant,
            harness.observer,
            daemon.otel_runtime,
        )
        assert [credential.kind for credential in plan.channel_credentials] == [
            ChannelKind.HOOK,
            ChannelKind.OTEL,
        ]
    finally:
        await daemon.aclose()


def test_native_otel_receiver_refuses_non_loopback() -> None:
    with pytest.raises(ValueError, match="loopback"):
        NativeOtelRuntime(lambda _participant, _channel: None, bind_host="0.0.0.0")


def test_native_otel_records_are_deeply_immutable_and_bounded() -> None:
    participant = type("P", (), {"id": "participant", "harness": "acme"})()
    body = _payload(participant, [_record(native_id="one", export_id="one")])
    record = otel_bounds.decode_otlp_json(body)[0]
    assert (
        record.timestamp_unix_nano,
        record.observed_timestamp_unix_nano,
        record.trace_id,
        record.span_id,
        record.severity_number,
        record.severity_text,
    ) == (42, 43, "trace", "span", 9, "INFO")
    with pytest.raises(TypeError):
        record.attributes["fixture.native"] = "other"
    nested = replace(record, body={"nested": ["value"]})
    with pytest.raises(TypeError):
        nested.body["nested"] = ()
    too_many = json.loads(body)
    too_many["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["attributes"] = [
        _value(f"key-{index}", "value") for index in range(65)
    ]
    with pytest.raises(otel_bounds.OtelIngressError, match="too many attributes"):
        otel_bounds.decode_otlp_json(
            json.dumps(too_many).encode(), bounds=OtelBounds(max_attributes=64)
        )


@pytest.mark.asyncio
async def test_native_otel_runtime_shutdown_is_idempotent_and_leaves_provider_alone() -> None:
    from opentelemetry import trace

    before = trace.get_tracer_provider()
    runtime = NativeOtelRuntime(lambda _participant, _channel: None)
    await runtime.start({})
    await runtime.aclose()
    await runtime.aclose()
    assert trace.get_tracer_provider() is before


@pytest.mark.asyncio
async def test_native_otel_decoder_timeout_preserves_cancellation() -> None:
    def slow(_context: OtelDecodeContext) -> tuple[ChannelFact, ...]:
        time.sleep(0.05)
        return ()

    channel = _channel(decoder=slow)
    runtime = NativeOtelRuntime(lambda _participant, _channel: None)
    runtime._callbacks = OtelCallbackRunner(max_in_flight=1, decoder_timeout=0.01)
    runtime._inbox.register("participant", channel.declaration)
    record = _record(native_id="slow", export_id="slow")
    from theater.harness.channels.otel.bounds import decode_otlp_json

    decoded = decode_otlp_json(
        _payload(type("P", (), {"id": "participant", "harness": "acme"})(), [record])
    )[0]
    runtime._inbox.enqueue(
        "participant",
        channel.declaration,
        OtelDelivery(OtelSignal.LOGS, "tool.finished", decoded, "slow", "slow:0"),
    )
    from theater.harness.channels.otel.source import OtelSource

    source = OtelSource(
        inbox=runtime._inbox,
        callbacks=runtime._callbacks,
        participant_id="participant",
        harness="acme",
        channel=channel,
    )
    try:
        assert (await source.read()).error_code == "otel_decode_failed"
        task = asyncio.create_task(source.read())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await source.aclose()
        await runtime.aclose()
