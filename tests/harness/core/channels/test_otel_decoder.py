"""The OTLP decoder: text and nesting bounds, immutable records and timeout cancellation."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import pytest

from tests.support.otel_channel_rig import (
    otel_channel,
    otel_manifest,
    otel_payload,
    otel_record,
    otel_value,
    post,
    prepare,
)
from theater.daemon.server import Daemon
from theater.harness.channels.otel import NativeOtelRuntime
from theater.harness.channels.otel import bounds as otel_bounds
from theater.harness.channels.otel.callbacks import OtelCallbackRunner
from theater.harness.channels.otel.inbox import OtelDelivery
from theater.harness.contracts.callbacks import OtelDecodeContext
from theater.harness.contracts.channels import ChannelFact, OtelBounds, OtelProtocol, OtelSignal
from theater.harness.manifests.compiler import compile_manifest


@pytest.mark.asyncio
async def test_native_otel_missing_optional_decoder_degrades_only_channel(
    theater_home,
    tmp_path,
    monkeypatch,
) -> None:
    harness = compile_manifest("acme", otel_manifest(protocol=OtelProtocol.OTLP_HTTP_PROTOBUF))
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant, credential = await prepare(daemon, harness, tmp_path)

    def missing(_name: str):
        raise ImportError("not installed")

    monkeypatch.setattr(otel_bounds.importlib, "import_module", missing)
    try:
        rejected = await post(
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


def test_native_otel_decoder_enforces_text_and_nesting_bounds() -> None:
    participant = type("P", (), {"id": "p", "harness": "a"})()
    text_payload = json.loads(
        otel_payload(participant, [otel_record(native_id="one", export_id="one")])
    )
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

    integer_payload = json.loads(
        otel_payload(participant, [otel_record(native_id="one", export_id="one")])
    )
    integer_payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["attributes"].append(
        {"key": "fixture.integer", "value": {"intValue": str(1 << 63)}}
    )
    with pytest.raises(otel_bounds.OtelIngressError, match="integer value is out of range"):
        otel_bounds.decode_otlp_json(json.dumps(integer_payload).encode())

    nested_payload = json.loads(
        otel_payload(participant, [otel_record(native_id="one", export_id="one")])
    )
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


def test_native_otel_records_are_deeply_immutable_and_bounded() -> None:
    participant = type("P", (), {"id": "participant", "harness": "acme"})()
    body = otel_payload(participant, [otel_record(native_id="one", export_id="one")])
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
        otel_value(f"key-{index}", "value") for index in range(65)
    ]
    with pytest.raises(otel_bounds.OtelIngressError, match="too many attributes"):
        otel_bounds.decode_otlp_json(
            json.dumps(too_many).encode(), bounds=OtelBounds(max_attributes=64)
        )


@pytest.mark.asyncio
async def test_native_otel_decoder_timeout_preserves_cancellation() -> None:
    def slow(_context: OtelDecodeContext) -> tuple[ChannelFact, ...]:
        time.sleep(0.05)
        return ()

    channel = otel_channel(decoder=slow)
    runtime = NativeOtelRuntime(lambda _participant, otel_channel: None)
    runtime._callbacks = OtelCallbackRunner(max_in_flight=1, decoder_timeout=0.01)
    runtime._inbox.register("participant", channel.declaration)
    record = otel_record(native_id="slow", export_id="slow")
    from theater.harness.channels.otel.bounds import decode_otlp_json

    decoded = decode_otlp_json(
        otel_payload(type("P", (), {"id": "participant", "harness": "acme"})(), [record])
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
