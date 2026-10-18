"""Manifests, receivers and posting helpers for the native OTLP channel tests."""

from __future__ import annotations

import asyncio
import json

from tests.support.channel_fixtures import launch_plan, primary_source, prompt_screen
from theater.daemon.server import Daemon
from theater.daemon.spawning.planning import (
    install_otel_plan,
    record_launch_identity,
    write_plan_files,
)
from theater.harness.contracts.callbacks import (
    HookInstallOverlay,
    OtelCorrelationContext,
    OtelDecodeContext,
    OtelInstallContext,
    OtelInstallOverlay,
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
from theater.harness.contracts.launch import LaunchPlan
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
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.trajectory.enums import TrajectoryKind


def correlate(context: OtelCorrelationContext) -> str:
    value = context.record.attributes.get("fixture.native")
    if not isinstance(value, str):
        raise TypeError("native identity is absent")
    return value


def otel_decode(context: OtelDecodeContext) -> tuple[ChannelFact, ...]:
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


def install(context: OtelInstallContext) -> OtelInstallOverlay:
    assert not hasattr(context, "token")
    config = context.token_file.with_name("native-otel.json")
    return OtelInstallOverlay(
        env={"ACME_NATIVE_OTEL_CONFIG": str(config)},
        files={config: f"{context.endpoint}:{context.auth_header}:{context.token_file}"},
        credential_header_env="ACME_NATIVE_OTEL_HEADERS",
    )


def otel_channel(
    *,
    max_queue: int = 3,
    max_records: int = 3,
    max_payload_bytes: int = 4096,
    protocol: OtelProtocol = OtelProtocol.OTLP_HTTP_JSON,
    decoder=otel_decode,
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
                correlation=correlate,
            ),
        ),
        installer=install,
    )


def otel_manifest(*, primary: bool = True, **kwargs) -> HarnessManifest:
    return HarnessManifest(
        api_version=MANIFEST_API_VERSION,
        binary="acme",
        icon="A",
        launch=LaunchManifest(planner=launch_plan, approvals=("manual",)),
        observation=ObservationManifest(
            primary=(
                SourceManifest(
                    factory=primary_source,
                    channel=ChannelDeclaration(id="primary", kind=ChannelKind.TRANSCRIPT),
                )
                if primary
                else None
            ),
            screen=ScreenManifest(classifier=prompt_screen),
            enrichments=(otel_channel(**kwargs),),
        ),
    )


def otel_value(key: str, value: object) -> dict[str, object]:
    if isinstance(value, str):
        typed = {"stringValue": value}
    elif type(value) is int:
        typed = {"intValue": str(value)}
    else:
        raise TypeError("fixture supports strings and integers")
    return {"key": key, "value": typed}


def otel_record(
    *,
    native_id: str,
    export_id: str,
    revision: int = 0,
    mode: str | None = None,
) -> dict[str, object]:
    attributes = [
        otel_value("fixture.signal", "tool.finished"),
        otel_value("fixture.export", export_id),
        otel_value("fixture.native", native_id),
        otel_value("fixture.revision", revision),
    ]
    if mode is not None:
        attributes.append(otel_value("fixture.mode", mode))
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


def otel_payload(
    participant, records: list[dict[str, object]], *, harness: str | None = None
) -> bytes:
    return json.dumps(
        {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            otel_value("fixture.participant", participant.id),
                            otel_value("fixture.harness", harness or participant.harness),
                            otel_value("fixture.channel", "native-otel"),
                        ]
                    },
                    "scopeLogs": [{"logRecords": records}],
                }
            ]
        }
    ).encode()


def hook_channel() -> HookChannelManifest:
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


def endpoint_parts(endpoint: str) -> tuple[str, int]:
    host_port = endpoint.removeprefix("http://")
    host, port = host_port.rsplit(":", 1)
    return host, int(port)


async def post(
    endpoint: str,
    body: bytes,
    *,
    token: str | None = None,
    path: str = "/v1/logs",
    content_type: str = "application/json",
    method: str = "POST",
    extra_headers: tuple[tuple[str, str], ...] = (),
) -> tuple[int, dict[str, str], bytes]:
    host, port = endpoint_parts(endpoint)
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


async def prepare(daemon: Daemon, harness, tmp_path, *, pid: str = "p-otel"):
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
