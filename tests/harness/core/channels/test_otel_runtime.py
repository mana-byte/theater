"""The native OTLP channel runtime: credentials, restart, installer environment and shutdown."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from tests.support.channel_fixtures import PrimarySource
from tests.support.otel_channel_rig import (
    endpoint_parts,
    hook_channel,
    otel_manifest,
    otel_payload,
    otel_record,
    post,
    prepare,
)
from theater.constants.daemon import CHANNEL_OTEL_RECEIVER_PORT_META_KEY
from theater.daemon.server import Daemon
from theater.daemon.spawning.planning import (
    install_hook_plan,
    install_otel_plan,
    validate_receipt_plan,
)
from theater.harness.channels import CompositeSource
from theater.harness.channels.otel import NativeOtelRuntime
from theater.harness.contracts.channels import ChannelDeclaration, ChannelKind
from theater.harness.contracts.launch import ChannelCredential, LaunchPlan
from theater.harness.contracts.manifest import OtelChannelManifest
from theater.harness.contracts.source import Batch
from theater.harness.manifests.compiler import compile_manifest
from theater.models import BadRequest


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
async def test_native_otel_restores_credentials_after_restart(theater_home, tmp_path) -> None:
    harness = compile_manifest("acme", otel_manifest())
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant, credential = await prepare(daemon, harness, tmp_path)
    endpoint = daemon.otel_runtime.endpoint
    config = credential.token_path.with_name("native-otel.json")
    assert endpoint in config.read_text()
    await daemon.aclose()

    restarted = Daemon(harnesses={"acme": harness})
    await restarted.start()
    try:
        assert restarted.otel_runtime.endpoint == endpoint
        assert endpoint in config.read_text()
        payload = otel_payload(participant, [otel_record(native_id="after", export_id="after")])
        assert (await post(endpoint, payload, token=credential.token))[0] == 200
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
    harness = compile_manifest("acme", otel_manifest())
    blocker = await asyncio.start_server(lambda _reader, _writer: None, host="127.0.0.1", port=0)
    port = blocker.sockets[0].getsockname()[1]
    daemon = Daemon(harnesses={"acme": harness})
    daemon.store.set_meta(CHANNEL_OTEL_RECEIVER_PORT_META_KEY, str(port))
    await daemon.start()
    participant = daemon.registry.create_spawned(harness="acme", cwd=str(tmp_path), pid="blocked")
    try:
        assert daemon.otel_runtime.available is True
        replacement_port = endpoint_parts(daemon.otel_runtime.endpoint)[1]
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
    harness = compile_manifest("acme", otel_manifest())
    daemon = Daemon(harnesses={"acme": harness})
    daemon.store.set_meta(CHANNEL_OTEL_RECEIVER_PORT_META_KEY, "invalid")
    await daemon.start()
    try:
        port = endpoint_parts(daemon.otel_runtime.endpoint)[1]
        assert daemon.otel_runtime.available is True
        assert daemon.store.get_meta(CHANNEL_OTEL_RECEIVER_PORT_META_KEY) == str(port)
    finally:
        await daemon.aclose()


@pytest.mark.asyncio
async def test_native_otel_installer_preserves_inherited_exporter_environment(
    theater_home, tmp_path, monkeypatch
) -> None:
    harness = compile_manifest("acme", otel_manifest())
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
async def test_native_otel_disabled_keeps_primary_source_exact(theater_home, tmp_path) -> None:
    unavailable = OtelChannelManifest(
        declaration=ChannelDeclaration(id="native-otel", kind=ChannelKind.OTEL),
        unavailable_reason="not safe",
    )
    manifest = otel_manifest()
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
        assert isinstance(source, PrimarySource)
        assert await source.read() == Batch()
        await source.aclose()
    finally:
        await daemon.aclose()


@pytest.mark.asyncio
async def test_native_otel_only_source_emits_facts_without_identity_state(
    theater_home, tmp_path
) -> None:
    harness = compile_manifest("acme", otel_manifest(primary=False))
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant, credential = await prepare(daemon, harness, tmp_path)
    source = daemon.observer._open_source(participant.id, harness.observer)
    assert isinstance(source, CompositeSource)
    assert source._primary is None
    try:
        payload = otel_payload(
            participant, [otel_record(native_id="otel-only", export_id="otel-only")]
        )
        assert (await post(daemon.otel_runtime.endpoint, payload, token=credential.token))[0] == 200
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


@pytest.mark.asyncio
async def test_channel_credential_preflight_precedes_installation_and_appends_channels(
    theater_home, tmp_path
) -> None:
    manifest = otel_manifest()
    harness = compile_manifest(
        "acme",
        replace(
            manifest,
            observation=replace(
                manifest.observation,
                enrichments=(hook_channel(), *manifest.observation.enrichments),
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


@pytest.mark.asyncio
async def test_native_otel_runtime_shutdown_is_idempotent_and_leaves_provider_alone() -> None:
    from opentelemetry import trace

    before = trace.get_tracer_provider()
    runtime = NativeOtelRuntime(lambda _participant, otel_channel: None)
    await runtime.start({})
    await runtime.aclose()
    await runtime.aclose()
    assert trace.get_tracer_provider() is before
