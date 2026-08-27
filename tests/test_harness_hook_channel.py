"""Focused generic hook-channel flow tests."""

from __future__ import annotations

import asyncio
import stat
import threading
import time
from collections.abc import Mapping, Sequence

import pytest

from theater.client import DaemonClient
from theater.daemon.lock import DaemonLock
from theater.daemon.persistence.store import Store
from theater.daemon.server import Daemon
from theater.daemon.spawning.planning import (
    install_hook_plan,
    record_launch_identity,
    write_plan_files,
)
from theater.harness.builtin.plugins.claude.manifest import MANIFEST as CLAUDE_MANIFEST
from theater.harness.builtin.plugins.codex.manifest import MANIFEST as CODEX_MANIFEST
from theater.harness.builtin.plugins.opencode.manifest import MANIFEST as OPENCODE_MANIFEST
from theater.harness.builtin.plugins.vibe.manifest import MANIFEST as VIBE_MANIFEST
from theater.harness.channels import CompositeSource, HookRuntime
from theater.harness.channels.hooks import HookDelivery, HookInbox
from theater.harness.channels.hooks.callbacks import HookCallbackRunner
from theater.harness.contracts.callbacks import (
    HookCorrelationContext,
    HookDecodeContext,
    HookInstallContext,
    HookInstallOverlay,
    LaunchContext,
    ScreenContext,
)
from theater.harness.contracts.channels import (
    ChannelBounds,
    ChannelCapability,
    ChannelDeclaration,
    ChannelFact,
    ChannelKind,
    HookBinding,
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
    ScreenManifest,
    SourceManifest,
)
from theater.harness.contracts.observation import ScreenConfidence, ScreenKind, ScreenReading
from theater.harness.contracts.source import Batch, Source
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.harness.manifests.compiler import compile_manifest
from theater.harness.manifests.validation import ManifestValidationError
from theater.protocol import RemoteError
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


def _correlation(context: HookCorrelationContext) -> str:
    native_id = context.payload.get("native_id")
    if not isinstance(native_id, str):
        raise TypeError("missing native_id")
    return native_id


def _decode(context: HookDecodeContext) -> tuple[ChannelFact, ...]:
    native_id = context.native_id
    if native_id == "boom":
        raise RuntimeError("decoder failed")
    if context.payload.get("mode") == "wrong":
        native_id = "other"
    revision = context.payload.get("revision", 0)
    if type(revision) is not int:
        raise TypeError("revision must be an integer")
    return (
        ChannelFact(
            SignalKind.TOOL,
            TrajectoryFact(kind=TrajectoryKind.TOOL_CALL, native_id=native_id, revision=revision),
        ),
    )


def _install(context: HookInstallContext) -> HookInstallOverlay:
    config = context.token_file.with_name("native-hook.json")
    return HookInstallOverlay(
        env={"ACME_HOOK_CONFIG": str(config)},
        files={config: f"{context.participant_id}:{context.channel_id}:{context.token_file}"},
    )


def _manifest(
    *,
    max_queue: int = 2,
    primary: bool = True,
    correlation=_correlation,
    decoder=_decode,
) -> HarnessManifest:
    channel = HookChannelManifest(
        declaration=ChannelDeclaration(
            id="native-hooks",
            kind=ChannelKind.HOOK,
            capabilities=(ChannelCapability(SignalKind.TOOL, SignalOwnership.ENRICHMENT),),
            bounds=ChannelBounds(max_queue=max_queue, max_payload_bytes=256),
        ),
        bindings=(
            HookBinding(
                event="tool.finished",
                signals=(SignalKind.TOOL,),
                decoder=decoder,
                correlation=correlation,
            ),
        ),
        installer=_install,
    )
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
            enrichments=(channel,),
        ),
    )


def _hook_channel(harness) -> HookChannelManifest:
    channel = harness.observer.enrichment_manifests()[0]
    assert isinstance(channel, HookChannelManifest)
    return channel


@pytest.mark.parametrize(
    ("name", "built"),
    (
        ("claude", CLAUDE_MANIFEST),
        ("codex", CODEX_MANIFEST),
        ("opencode", OPENCODE_MANIFEST),
        ("vibe", VIBE_MANIFEST),
    ),
)
def test_shipped_manifests_declare_native_hooks_unavailable(name, built) -> None:
    compiled = compile_manifest(name, built)
    channels = tuple(
        channel
        for channel in compiled.observer.enrichment_manifests()
        if isinstance(channel, HookChannelManifest)
    )

    assert len(channels) == 1
    channel = channels[0]
    assert isinstance(channel, HookChannelManifest)
    assert channel.declaration.id == "native-hooks"
    assert channel.declaration.kind is ChannelKind.HOOK
    assert channel.declaration.capabilities == ()
    assert channel.bindings == ()
    assert channel.installer is None
    assert channel.unavailable_reason


def test_hook_manifest_rejects_unavailable_active_and_duplicate_events() -> None:
    built = _manifest()
    channel = built.observation.hook_channels[0]
    invalid = HookChannelManifest(
        declaration=channel.declaration,
        bindings=channel.bindings,
        installer=channel.installer,
        unavailable_reason="not installed",
    )
    with pytest.raises(ManifestValidationError) as raised:
        compile_manifest(
            "acme",
            HarnessManifest(
                api_version=built.api_version,
                binary=built.binary,
                icon=built.icon,
                launch=built.launch,
                observation=ObservationManifest(
                    primary=built.observation.primary,
                    screen=built.observation.screen,
                    enrichments=(invalid,),
                ),
            ),
        )
    assert raised.value.path == "observation.enrichments[0].bindings"

    empty = HookChannelManifest(declaration=channel.declaration)
    with pytest.raises(ManifestValidationError) as raised:
        compile_manifest(
            "acme",
            HarnessManifest(
                api_version=built.api_version,
                binary=built.binary,
                icon=built.icon,
                launch=built.launch,
                observation=ObservationManifest(
                    primary=built.observation.primary,
                    screen=built.observation.screen,
                    enrichments=(empty,),
                ),
            ),
        )
    assert raised.value.path == "observation.enrichments[0].bindings"


def test_hook_inbox_transport_dedupes_only_delivery_id() -> None:
    channel = _manifest().observation.hook_channels[0].declaration
    inbox = HookInbox()
    assert (
        inbox.enqueue(
            "participant",
            channel,
            HookDelivery(
                event="tool.finished",
                payload={"native_id": "shared"},
                native_id="shared",
                delivery_id="first",
            ),
        ).duplicate
        is False
    )
    assert (
        inbox.enqueue(
            "participant",
            channel,
            HookDelivery(
                event="other.event",
                payload={"native_id": "shared"},
                native_id="shared",
                delivery_id="second",
            ),
        ).duplicate
        is False
    )
    assert (
        inbox.enqueue(
            "participant",
            channel,
            HookDelivery(
                event="tool.finished",
                payload={"native_id": "other"},
                native_id="other",
                delivery_id="first",
            ),
        ).duplicate
        is True
    )
    assert [delivery.event for delivery in inbox.drain("participant", channel.id)] == [
        "tool.finished",
        "other.event",
    ]


@pytest.mark.asyncio
async def test_hook_source_rejects_undeclared_signal_and_malformed_output() -> None:
    def undeclared(context: HookDecodeContext) -> tuple[ChannelFact, ...]:
        return (
            ChannelFact(
                SignalKind.CONTENT,
                TrajectoryFact(kind=TrajectoryKind.TOOL_CALL, native_id=context.native_id),
            ),
        )

    channel = _manifest(decoder=undeclared).observation.hook_channels[0]
    runtime = HookRuntime(lambda _participant_id, _channel_id: True)
    try:
        runtime.enqueue(
            participant_id="participant",
            channel=channel,
            event="tool.finished",
            payload={"native_id": "undeclared"},
            delivery_id="undeclared",
            native_id="undeclared",
        )
        source = runtime.open_source(participant_id="participant", channel=channel)
        assert (await source.read()).error_code == "hook_decode_failed"

        def malformed(_context: HookDecodeContext) -> tuple[object, ...]:
            return ("not-a-channel-fact",)

        malformed_channel = _manifest(decoder=malformed).observation.hook_channels[0]
        runtime.enqueue(
            participant_id="malformed",
            channel=malformed_channel,
            event="tool.finished",
            payload={"native_id": "malformed"},
            delivery_id="malformed",
            native_id="malformed",
        )
        malformed_source = runtime.open_source(
            participant_id="malformed", channel=malformed_channel
        )
        assert (await malformed_source.read()).error_code == "hook_decode_failed"
        await source.aclose()
        await malformed_source.aclose()
    finally:
        await runtime.aclose()


async def _client(daemon: Daemon) -> DaemonClient:
    client = DaemonClient(autostart=False)
    await client.connect()
    return client


async def _event(client: DaemonClient, *, pid: str, token: str, native_id: str, **extra):
    payload = {"native_id": native_id, **extra.pop("payload", {})}
    return await client.call(
        "harness.event",
        id=pid,
        token=token,
        channel="native-hooks",
        event="tool.finished",
        payload=payload,
        **extra,
    )


@pytest.mark.asyncio
async def test_hook_transport_credentials_bounds_decode_and_cleanup(  # noqa: PLR0915
    theater_home, tmp_path
) -> None:
    harness = compile_manifest("acme", _manifest(max_queue=2))
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant = daemon.registry.create_spawned(harness="acme", cwd=str(tmp_path), pid="p-hook")
    plan = install_hook_plan(LaunchPlan(argv=["acme"]), participant, harness.observer)
    record_launch_identity(participant, plan, daemon.registry)
    write_plan_files(plan)
    credential = plan.channel_credentials[0]
    assert stat.S_IMODE(credential.token_path.stat().st_mode) == 0o600
    assert credential.token not in (credential.token_path.with_name("native-hook.json")).read_text()
    assert plan.env["ACME_HOOK_CONFIG"].endswith("native-hook.json")

    client = await _client(daemon)
    try:
        first = await _event(
            client,
            pid=participant.id,
            token=credential.token,
            native_id="one",
            delivery_id="delivery-one",
        )
        duplicate = await _event(
            client,
            pid=participant.id,
            token=credential.token,
            native_id="one",
            delivery_id="delivery-one",
        )
        native_duplicate = await _event(
            client,
            pid=participant.id,
            token=credential.token,
            native_id="one",
            delivery_id="delivery-one-retry",
        )
        overflow = await _event(
            client,
            pid=participant.id,
            token=credential.token,
            native_id="two",
            delivery_id="delivery-two",
        )
        assert first == {"ok": True, "duplicate": False, "dropped": False}
        assert duplicate == {"ok": True, "duplicate": True, "dropped": False}
        assert native_duplicate == {"ok": True, "duplicate": False, "dropped": False}
        assert overflow == {"ok": True, "duplicate": False, "dropped": True}
        assert await _event(
            client,
            pid=participant.id,
            token=credential.token,
            native_id="two",
            delivery_id="delivery-two",
        ) == {"ok": True, "duplicate": True, "dropped": False}

        source = daemon.observer._open_source(participant.id, harness.observer)
        assert isinstance(source, CompositeSource)
        batch = await source.read()
        assert [fact.native_id for fact in batch.trajectory] == ["one"]
        hook_source = source._enrichments[0].source
        health = hook_source.channel_health()
        assert health is not None and health.dropped == 1
        assert source.channel_health()[0].dropped == 1

        await _event(
            client,
            pid=participant.id,
            token=credential.token,
            native_id="boom",
            delivery_id="delivery-boom",
        )
        await source.read()
        assert hook_source.channel_health().state.value == "degraded"
        await _event(
            client,
            pid=participant.id,
            token=credential.token,
            native_id="wrong",
            delivery_id="delivery-wrong",
            payload={"mode": "wrong"},
        )
        await source.read()
        assert hook_source.channel_health().state.value == "degraded"

        with pytest.raises(RemoteError, match="credential is invalid"):
            await _event(client, pid=participant.id, token="forged", native_id="x")
        with pytest.raises(RemoteError, match="non-empty string"):
            await _event(client, pid=participant.id, token="", native_id="x")
        with pytest.raises(RemoteError, match="credential is invalid"):
            await _event(client, pid=participant.id, token="x" * 129, native_id="x")
        other = daemon.registry.create_spawned(harness="acme", cwd=str(tmp_path), pid="p-other")
        with pytest.raises(RemoteError, match="credential is invalid"):
            await _event(client, pid=other.id, token=credential.token, native_id="x")
        with pytest.raises(RemoteError, match="credential is invalid"):
            await client.call(
                "harness.event",
                id=participant.id,
                token=credential.token,
                channel="other-channel",
                event="tool.finished",
                payload={"native_id": "x"},
            )
        with pytest.raises(RemoteError, match="harness does not match"):
            await _event(
                client,
                pid=participant.id,
                token=credential.token,
                native_id="x",
                harness="other",
            )
        with pytest.raises(RemoteError, match="payload exceeds"):
            await client.call(
                "harness.event",
                id=participant.id,
                token=credential.token,
                channel="native-hooks",
                event="tool.finished",
                payload={"native_id": "x", "large": "x" * 512},
            )
        with pytest.raises(RemoteError, match="JSON object"):
            await client.call(
                "harness.event",
                id=participant.id,
                token=credential.token,
                channel="native-hooks",
                event="tool.finished",
                payload=[],
            )
        with pytest.raises(RemoteError, match="too many attributes"):
            await client.call(
                "harness.event",
                id=participant.id,
                token=credential.token,
                channel="native-hooks",
                event="tool.finished",
                payload={f"key-{i}": i for i in range(200)},
            )
        nested = {"native_id": "x"}
        for _ in range(10):
            nested = {"next": nested}
        with pytest.raises(RemoteError, match="nesting"):
            await client.call(
                "harness.event",
                id=participant.id,
                token=credential.token,
                channel="native-hooks",
                event="tool.finished",
                payload=nested,
            )
        with pytest.raises(RemoteError, match="not declared"):
            await client.call(
                "harness.event",
                id=participant.id,
                token=credential.token,
                channel="native-hooks",
                event="unknown",
                payload={"native_id": "x"},
            )

        daemon.registry.mark_dead(participant.id)
        assert not credential.token_path.exists()
        assert hook_source.channel_health() is None
        await source.aclose()
    finally:
        await client.aclose()
        await daemon.aclose()


@pytest.mark.asyncio
async def test_hook_credential_survives_runtime_restart(theater_home, tmp_path) -> None:
    harness = compile_manifest("acme", _manifest())
    daemon = Daemon(harnesses={"acme": harness})
    participant = daemon.registry.create_spawned(harness="acme", cwd=str(tmp_path), pid="p-restart")
    plan = install_hook_plan(LaunchPlan(argv=["acme"]), participant, harness.observer)
    record_launch_identity(participant, plan, daemon.registry)
    write_plan_files(plan)
    credential = plan.channel_credentials[0]
    await daemon.aclose()

    restarted = Daemon(harnesses={"acme": harness})
    await restarted.start()
    client = await _client(restarted)
    try:
        result = await _event(
            client,
            pid=participant.id,
            token=credential.token,
            native_id="after-restart",
        )
        assert result["ok"] is True
        source = restarted.observer._open_source(participant.id, harness.observer)
        assert source is not None
        assert [fact.native_id for fact in (await source.read()).trajectory] == ["after-restart"]
        await source.aclose()
    finally:
        await client.aclose()
        await restarted.aclose()


@pytest.mark.asyncio
async def test_hook_correlation_is_admitted_once_before_enqueue(theater_home, tmp_path) -> None:
    correlations: list[HookCorrelationContext] = []
    decoded: list[str] = []

    def correlation(context: HookCorrelationContext) -> str:
        correlations.append(context)
        native_id = context.payload["native_id"]
        if native_id == "explode":
            raise RuntimeError("secret payload detail")
        if native_id == "blank":
            return " "
        if not isinstance(native_id, str):
            raise TypeError("missing native id")
        return native_id

    def decode(context: HookDecodeContext) -> tuple[ChannelFact, ...]:
        decoded.append(context.native_id)
        return (
            ChannelFact(
                SignalKind.TOOL,
                TrajectoryFact(kind=TrajectoryKind.TOOL_CALL, native_id=context.native_id),
            ),
        )

    harness = compile_manifest("acme", _manifest(correlation=correlation, decoder=decode))
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant = daemon.registry.create_spawned(
        harness="acme", cwd=str(tmp_path), pid="p-correlation"
    )
    plan = install_hook_plan(LaunchPlan(argv=["acme"]), participant, harness.observer)
    record_launch_identity(participant, plan, daemon.registry)
    write_plan_files(plan)
    credential = plan.channel_credentials[0]
    client = await _client(daemon)
    try:
        assert await _event(
            client,
            pid=participant.id,
            token=credential.token,
            native_id="accepted",
            delivery_id="accepted-delivery",
        ) == {"ok": True, "duplicate": False, "dropped": False}
        assert len(correlations) == 1
        source = daemon.observer._open_source(participant.id, harness.observer)
        assert source is not None
        assert [fact.native_id for fact in (await source.read()).trajectory] == ["accepted"]
        assert decoded == ["accepted"]
        assert await _event(
            client,
            pid=participant.id,
            token=credential.token,
            native_id="explode",
            delivery_id="accepted-delivery",
        ) == {"ok": True, "duplicate": True, "dropped": False}
        assert len(correlations) == 1

        before = sum(
            row["kind"] == "agent.harness_event" for row in daemon.store.bus_tail(limit=1000)
        )
        with pytest.raises(RemoteError, match="correlation is invalid") as raised:
            await _event(
                client,
                pid=participant.id,
                token=credential.token,
                native_id="explode",
                delivery_id="explode-delivery",
            )
        assert "secret payload detail" not in str(raised.value)
        with pytest.raises(RemoteError, match="correlation is invalid"):
            await _event(
                client,
                pid=participant.id,
                token=credential.token,
                native_id="blank",
                delivery_id="blank-delivery",
            )
        assert len(correlations) == 3
        assert (
            sum(row["kind"] == "agent.harness_event" for row in daemon.store.bus_tail(limit=1000))
            == before
        )
        assert (await source.read()).trajectory == ()
        await source.aclose()
    finally:
        await client.aclose()
        await daemon.aclose()


@pytest.mark.asyncio
async def test_concurrent_retry_waits_for_a_real_accepted_delivery(theater_home, tmp_path) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def correlation(context: HookCorrelationContext) -> str:
        nonlocal calls
        with calls_lock:
            calls += 1
            call = calls
        if call == 1:
            entered.set()
            release.wait(timeout=1)
            raise RuntimeError("first delivery failed")
        return str(context.payload["native_id"])

    harness = compile_manifest("acme", _manifest(correlation=correlation))
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant = daemon.registry.create_spawned(
        harness="acme", cwd=str(tmp_path), pid="p-concurrent-retry"
    )
    plan = install_hook_plan(LaunchPlan(argv=["acme"]), participant, harness.observer)
    record_launch_identity(participant, plan, daemon.registry)
    credential = plan.channel_credentials[0]
    client = await _client(daemon)
    try:
        first = asyncio.create_task(
            _event(
                client,
                pid=participant.id,
                token=credential.token,
                native_id="same",
                delivery_id="same-delivery",
            )
        )
        assert await asyncio.to_thread(entered.wait, 1)
        second = await _event(
            client,
            pid=participant.id,
            token=credential.token,
            native_id="same",
            delivery_id="same-delivery",
        )
        release.set()
        with pytest.raises(RemoteError, match="correlation is invalid"):
            await first
        assert second == {"ok": True, "duplicate": False, "dropped": False}
        source = daemon.observer._open_source(participant.id, harness.observer)
        assert source is not None
        assert [fact.native_id for fact in (await source.read()).trajectory] == ["same"]
        await source.aclose()
    finally:
        release.set()
        await client.aclose()
        await daemon.aclose()


@pytest.mark.asyncio
async def test_hook_semantic_dedupe_survives_source_recreation(theater_home, tmp_path) -> None:
    harness = compile_manifest("acme", _manifest())
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant = daemon.registry.create_spawned(harness="acme", cwd=str(tmp_path), pid="p-dedupe")
    plan = install_hook_plan(LaunchPlan(argv=["acme"]), participant, harness.observer)
    record_launch_identity(participant, plan, daemon.registry)
    write_plan_files(plan)
    credential = plan.channel_credentials[0]
    client = await _client(daemon)
    try:
        await _event(
            client,
            pid=participant.id,
            token=credential.token,
            native_id="shared",
            delivery_id="first",
        )
        first = daemon.observer._open_source(participant.id, harness.observer)
        assert first is not None
        assert [(fact.native_id, fact.revision) for fact in (await first.read()).trajectory] == [
            ("shared", 0)
        ]
        await first.aclose()

        await _event(
            client,
            pid=participant.id,
            token=credential.token,
            native_id="shared",
            delivery_id="same-fact-new-delivery",
        )
        reopened = daemon.observer._open_source(participant.id, harness.observer)
        assert reopened is not None
        assert (await reopened.read()).trajectory == ()
        await _event(
            client,
            pid=participant.id,
            token=credential.token,
            native_id="shared",
            delivery_id="next-revision",
            payload={"revision": 1},
        )
        assert [(fact.native_id, fact.revision) for fact in (await reopened.read()).trajectory] == [
            ("shared", 1)
        ]
        await reopened.aclose()
    finally:
        await client.aclose()
        await daemon.aclose()


@pytest.mark.asyncio
async def test_hook_correlation_timeout_is_generic_and_not_audited(theater_home, tmp_path) -> None:
    def slow_correlation(_context: HookCorrelationContext) -> str:
        time.sleep(0.05)
        return "late"

    harness = compile_manifest("acme", _manifest(correlation=slow_correlation))
    daemon = Daemon(harnesses={"acme": harness})
    original_runner = daemon.hook_runtime._callbacks
    daemon.hook_runtime._callbacks = HookCallbackRunner(
        max_in_flight=1,
        correlation_timeout=0.01,
        decoder_timeout=0.1,
    )
    await original_runner.aclose()
    await daemon.start()
    participant = daemon.registry.create_spawned(harness="acme", cwd=str(tmp_path), pid="p-timeout")
    plan = install_hook_plan(LaunchPlan(argv=["acme"]), participant, harness.observer)
    record_launch_identity(participant, plan, daemon.registry)
    write_plan_files(plan)
    credential = plan.channel_credentials[0]
    client = await _client(daemon)
    try:
        before = sum(
            row["kind"] == "agent.harness_event" for row in daemon.store.bus_tail(limit=1000)
        )
        with pytest.raises(RemoteError, match="correlation is invalid"):
            await _event(
                client,
                pid=participant.id,
                token=credential.token,
                native_id="late",
                delivery_id="late-delivery",
            )
        assert (
            sum(row["kind"] == "agent.harness_event" for row in daemon.store.bus_tail(limit=1000))
            == before
        )
    finally:
        await client.aclose()
        await daemon.aclose()


class _IndexedFacts(Sequence[ChannelFact]):
    def __init__(self, facts: tuple[ChannelFact, ...]) -> None:
        self._facts = facts

    def __len__(self) -> int:
        return len(self._facts)

    def __getitem__(self, index: int) -> ChannelFact:
        return self._facts[index]

    def __iter__(self):
        raise AssertionError("hook source must index bounded decoder output")


@pytest.mark.asyncio
async def test_hook_source_bounds_aggregate_output_and_requeues_deliveries() -> None:
    def decode(context: HookDecodeContext) -> Sequence[ChannelFact]:
        return _IndexedFacts(
            tuple(
                ChannelFact(
                    SignalKind.TOOL,
                    TrajectoryFact(
                        kind=TrajectoryKind.TOOL_CALL,
                        native_id=context.native_id,
                        revision=revision,
                    ),
                )
                for revision in range(4)
            )
        )

    manifest = _manifest(max_queue=2, decoder=decode)
    channel = manifest.observation.hook_channels[0]
    runtime = HookRuntime(lambda _participant_id, _channel_id: True)
    try:
        runtime.enqueue(
            participant_id="participant",
            channel=channel,
            event="tool.finished",
            payload={"native_id": "first"},
            delivery_id="first-delivery",
            native_id="first",
        )
        runtime.enqueue(
            participant_id="participant",
            channel=channel,
            event="tool.finished",
            payload={"native_id": "second"},
            delivery_id="second-delivery",
            native_id="second",
        )
        source = runtime.open_source(participant_id="participant", channel=channel)
        first = await source.read()
        assert [(fact.native_id, fact.revision) for fact in first.trajectory] == [
            ("first", 0),
            ("first", 1),
        ]
        second = await source.read()
        assert [(fact.native_id, fact.revision) for fact in second.trajectory] == [
            ("second", 0),
            ("second", 1),
        ]
        health = source.channel_health()
        assert health is not None and health.dropped == 4
        await source.aclose()
    finally:
        await runtime.aclose()


def test_hook_requeue_preserves_bound_and_oldest_delivery_order() -> None:
    declaration = _manifest(max_queue=2).observation.hook_channels[0].declaration
    inbox = HookInbox()
    for native_id in ("old-1", "old-2"):
        inbox.enqueue(
            "participant",
            declaration,
            HookDelivery("tool.finished", {}, native_id, native_id),
        )
    drained = inbox.drain("participant", declaration.id)
    for native_id in ("new-1", "new-2"):
        inbox.enqueue(
            "participant",
            declaration,
            HookDelivery("tool.finished", {}, native_id, native_id),
        )

    inbox.requeue("participant", declaration.id, drained)

    assert [item.native_id for item in inbox.drain("participant", declaration.id)] == [
        "old-1",
        "old-2",
    ]
    health = inbox.health("participant", declaration.id)
    assert health is not None and health.snapshot().dropped == 2


@pytest.mark.asyncio
async def test_hook_source_requires_credential_and_allows_hook_only_manifest(
    theater_home, tmp_path
) -> None:
    durable = compile_manifest("acme", _manifest())
    daemon = Daemon(harnesses={"acme": durable})
    participant = daemon.registry.create_spawned(
        harness="acme", cwd=str(tmp_path), pid="p-no-credential"
    )
    source = daemon.observer._open_source(participant.id, durable.observer)
    assert isinstance(source, _Primary)
    await source.aclose()
    await daemon.aclose()

    hook_only = compile_manifest("acme", _manifest(primary=False))
    daemon = Daemon(harnesses={"acme": hook_only})
    participant = daemon.registry.create_spawned(harness="acme", cwd=None, pid="p-hook-only")
    assert daemon.observer._open_source(participant.id, hook_only.observer) is None
    plan = install_hook_plan(LaunchPlan(argv=["acme"]), participant, hook_only.observer)
    record_launch_identity(participant, plan, daemon.registry)
    channel = _hook_channel(hook_only)
    daemon.hook_runtime.enqueue(
        participant_id=participant.id,
        channel=channel,
        event="tool.finished",
        payload={"native_id": "hook-only"},
        delivery_id="hook-only-delivery",
        native_id="hook-only",
    )
    source = daemon.observer._open_source(participant.id, hook_only.observer)
    assert isinstance(source, CompositeSource)
    assert source._primary is None
    assert [fact.native_id for fact in (await source.read()).trajectory] == ["hook-only"]
    await source.aclose()
    await daemon.aclose()


@pytest.mark.asyncio
async def test_shared_manifest_does_not_crosswire_two_daemon_hook_runtimes(
    theater_home, tmp_path
) -> None:
    harness = compile_manifest("acme", _manifest())
    first_lock = DaemonLock(tmp_path / "first.pid")
    second_lock = DaemonLock(tmp_path / "second.pid")
    first_lock.acquire()
    second_lock.acquire()
    first = Daemon(
        store=Store(tmp_path / "first.db"),
        harnesses={"acme": harness},
        lock=first_lock,
    )
    second = Daemon(
        store=Store(tmp_path / "second.db"),
        harnesses={"acme": harness},
        lock=second_lock,
    )
    first_source = None
    second_source = None
    try:
        first_participant = first.registry.create_spawned(
            harness="acme", cwd=str(tmp_path), pid="p-first-runtime"
        )
        second_participant = second.registry.create_spawned(
            harness="acme", cwd=str(tmp_path), pid="p-second-runtime"
        )
        for daemon, participant in ((first, first_participant), (second, second_participant)):
            plan = install_hook_plan(LaunchPlan(argv=["acme"]), participant, harness.observer)
            record_launch_identity(participant, plan, daemon.registry)
        channel = _hook_channel(harness)
        first.hook_runtime.enqueue(
            participant_id=first_participant.id,
            channel=channel,
            event="tool.finished",
            payload={"native_id": "first"},
            delivery_id="first-delivery",
            native_id="first",
        )
        first_source = first.observer._open_source(first_participant.id, harness.observer)
        second_source = second.observer._open_source(second_participant.id, harness.observer)
        assert first_source is not None and second_source is not None
        assert [fact.native_id for fact in (await first_source.read()).trajectory] == ["first"]
        assert (await second_source.read()).trajectory == ()
        second.hook_runtime.enqueue(
            participant_id=second_participant.id,
            channel=channel,
            event="tool.finished",
            payload={"native_id": "second"},
            delivery_id="second-delivery",
            native_id="second",
        )
        assert [fact.native_id for fact in (await second_source.read()).trajectory] == ["second"]
    finally:
        if first_source is not None:
            await first_source.aclose()
        if second_source is not None:
            await second_source.aclose()
        await first.aclose()
        await second.aclose()


def test_hook_contexts_freeze_only_json_values() -> None:
    payload = {"nested": ({"items": ["value"]},)}
    context = HookCorrelationContext(
        participant_id="participant",
        channel_id="native-hooks",
        event="tool.finished",
        payload=payload,
    )
    payload["nested"] = ()
    nested = context.payload["nested"]
    assert isinstance(nested, tuple)
    assert isinstance(nested[0], Mapping)
    assert nested[0]["items"] == ("value",)
    with pytest.raises(TypeError):
        nested[0]["items"] = ()
    with pytest.raises(TypeError, match="JSON-compatible"):
        HookCorrelationContext(
            participant_id="participant",
            channel_id="native-hooks",
            event="tool.finished",
            payload={"unsupported": {"value"}},
        )


@pytest.mark.asyncio
async def test_hook_callbacks_stay_off_loop_and_recover_queued_order() -> None:
    runner = HookCallbackRunner(max_in_flight=1, correlation_timeout=0.1, decoder_timeout=0.01)
    runtime = HookRuntime(lambda _participant_id, _channel_id: True, callback_runner=runner)

    def slow_correlation(_context: HookCorrelationContext) -> str:
        time.sleep(0.04)
        return "correlated"

    correlation_binding = (
        _manifest(correlation=slow_correlation).observation.hook_channels[0].bindings[0]
    )

    def decoder(context: HookDecodeContext) -> tuple[ChannelFact, ...]:
        if context.native_id == "slow":
            time.sleep(0.05)
        return (
            ChannelFact(
                SignalKind.TOOL,
                TrajectoryFact(kind=TrajectoryKind.TOOL_CALL, native_id=context.native_id),
            ),
        )

    started = time.monotonic()
    correlation = asyncio.create_task(
        runtime.correlate(
            correlation_binding,
            HookCorrelationContext(
                participant_id="participant",
                channel_id="native-hooks",
                event="tool.finished",
                payload={"native_id": "correlated"},
            ),
        )
    )
    await asyncio.sleep(0.005)
    assert time.monotonic() - started < 0.03
    assert await correlation == "correlated"

    timed_channel = _manifest(max_queue=3, decoder=decoder).observation.hook_channels[0]
    for native_id in ("slow", "second", "third"):
        runtime.enqueue(
            participant_id="participant",
            channel=timed_channel,
            event="tool.finished",
            payload={"native_id": native_id},
            delivery_id=native_id,
            native_id=native_id,
        )
    source = runtime.open_source(participant_id="participant", channel=timed_channel)
    started = time.monotonic()
    assert (await source.read()).error_code == "hook_decode_failed"
    assert time.monotonic() - started < 0.04
    assert (await source.read()).error_code == "hook_decode_failed"
    await asyncio.sleep(0.06)
    assert [fact.native_id for fact in (await source.read()).trajectory] == ["second", "third"]
    await source.aclose()
    await runtime.aclose()


@pytest.mark.asyncio
async def test_hook_source_cancellation_requeues_unrelated_deliveries() -> None:
    runner = HookCallbackRunner(max_in_flight=1, correlation_timeout=0.1, decoder_timeout=0.2)
    runtime = HookRuntime(lambda _participant_id, _channel_id: True, callback_runner=runner)

    def decoder(context: HookDecodeContext) -> tuple[ChannelFact, ...]:
        if context.native_id == "first":
            time.sleep(0.05)
        return (
            ChannelFact(
                SignalKind.TOOL,
                TrajectoryFact(kind=TrajectoryKind.TOOL_CALL, native_id=context.native_id),
            ),
        )

    channel = _manifest(decoder=decoder).observation.hook_channels[0]
    try:
        for native_id in ("first", "second"):
            runtime.enqueue(
                participant_id="participant",
                channel=channel,
                event="tool.finished",
                payload={"native_id": native_id},
                delivery_id=native_id,
                native_id=native_id,
            )
        source = runtime.open_source(participant_id="participant", channel=channel)
        read = asyncio.create_task(source.read())
        await asyncio.sleep(0.005)
        read.cancel()
        with pytest.raises(asyncio.CancelledError):
            await read
        await asyncio.sleep(0.06)
        assert [fact.native_id for fact in (await source.read()).trajectory] == ["first", "second"]
        await source.aclose()
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_hook_only_watch_leaves_transcript_identity_state_untouched(
    theater_home, tmp_path
) -> None:
    harness = compile_manifest("acme", _manifest(primary=False))
    daemon = Daemon(harnesses={"acme": harness})
    participant = daemon.registry.create_spawned(harness="acme", cwd=None, pid="p-hook-identity")
    plan = install_hook_plan(LaunchPlan(argv=["acme"]), participant, harness.observer)
    record_launch_identity(participant, plan, daemon.registry)
    observer = daemon.observer
    observer._failures._identity_lost.add(participant.id)
    observer._failures._identity_loss_replayed.add(participant.id)
    observer._attachments._receipt_candidates[participant.id] = ("/tmp/transcript", "session")

    async def sleep_once(_seconds: float) -> None:
        observer._stopping.set()

    observer._sleep = sleep_once
    try:
        await observer._watch_source(participant.id, "acme")
        assert participant.id in observer._failures._identity_lost
        assert participant.id in observer._failures._identity_loss_replayed
        assert participant.id in observer._attachments._receipt_candidates
    finally:
        await daemon.aclose()
