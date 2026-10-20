"""The hook source: declared signals, stale identity, manifest health and cancellation."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from tests.support.channel_fixtures import PrimarySource
from tests.support.hook_channel_rig import hook_client, hook_manifest
from theater.daemon.lock import DaemonLock
from theater.daemon.persistence.store import Store
from theater.daemon.server import Daemon
from theater.daemon.spawning.planning import install_hook_plan, record_launch_identity
from theater.harness.channels import CompositeSource, HookRuntime
from theater.harness.channels.hooks.callbacks import HookCallbackRunner
from theater.harness.contracts.callbacks import (
    HookAdmissionIdentity,
    HookCorrelationContext,
    HookDecodeContext,
)
from theater.harness.contracts.channels import ChannelFact, SignalKind
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.manifest import (
    HarnessManifest,
    HookChannelManifest,
    ObservationManifest,
)
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.harness.loading.models import LoadedPlugin
from theater.harness.manifests.compiler import compile_manifest
from theater.harness.registry import _PLUGINS, HARNESSES
from theater.trajectory.enums import TrajectoryKind


def _manifest_with_unavailable_hook() -> HarnessManifest:
    built = hook_manifest()
    channel = built.observation.hook_channels[0]
    return HarnessManifest(
        api_version=built.api_version,
        binary=built.binary,
        icon=built.icon,
        launch=built.launch,
        observation=ObservationManifest(
            primary=built.observation.primary,
            screen=built.observation.screen,
            enrichments=(
                HookChannelManifest(
                    declaration=channel.declaration,
                    unavailable_reason="native hooks are unavailable",
                ),
            ),
        ),
    )


def _hook_channel(harness) -> HookChannelManifest:
    channel = harness.observer.enrichment_manifests()[0]
    assert isinstance(channel, HookChannelManifest)
    return channel


@pytest.mark.asyncio
async def test_hook_source_rejects_undeclared_signal_and_malformed_output() -> None:
    def undeclared(context: HookDecodeContext) -> tuple[ChannelFact, ...]:
        return (
            ChannelFact(
                SignalKind.CONTENT,
                TrajectoryFact(kind=TrajectoryKind.TOOL_CALL, native_id=context.native_id),
            ),
        )

    channel = hook_manifest(decoder=undeclared).observation.hook_channels[0]
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

        malformed_channel = hook_manifest(decoder=malformed).observation.hook_channels[0]
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


@pytest.mark.asyncio
async def test_hook_source_drops_stale_admitted_identity_without_changing_legacy_delivery() -> None:
    admitted = HookAdmissionIdentity(
        harness="acme",
        session_id="session-a",
        session_correlation="exact",
        transcript_location="/tmp/a.jsonl",
    )
    current = {"participant": admitted}

    def identity_provider(participant_id: str) -> HookAdmissionIdentity | None:
        return current.get(participant_id)

    channel = hook_manifest().observation.hook_channels[0]
    runtime = HookRuntime(
        lambda _participant_id, _channel_id: True,
        identity_provider=identity_provider,
    )
    source = None
    try:
        runtime.enqueue(
            participant_id="participant",
            channel=channel,
            event="tool.finished",
            payload={"native_id": "admitted"},
            delivery_id="admitted",
            native_id="admitted",
            admission_identity=admitted,
        )
        current["participant"] = HookAdmissionIdentity(
            harness="acme",
            session_id="session-b",
            session_correlation="exact",
            transcript_location="/tmp/b.jsonl",
        )
        source = runtime.open_source(participant_id="participant", channel=channel)
        assert (await source.read()).trajectory == ()
        health = source.channel_health()
        assert health is not None
        assert health.dropped == 1
        assert "hook admission identity changed" in health.diagnostics

        # Existing callers that do not opt into admission identities retain
        # their queue/decode behaviour even when a runtime has a provider.
        runtime.enqueue(
            participant_id="participant",
            channel=channel,
            event="tool.finished",
            payload={"native_id": "legacy"},
            delivery_id="legacy",
            native_id="legacy",
        )
        assert [fact.native_id for fact in (await source.read()).trajectory] == ["legacy"]
    finally:
        if source is not None:
            await source.aclose()
        await runtime.aclose()


@pytest.mark.asyncio
async def test_hook_source_requires_credential_and_allows_hook_only_manifest(
    theater_home, tmp_path
) -> None:
    durable = compile_manifest("acme", hook_manifest())
    daemon = Daemon(harnesses={"acme": durable})
    participant = daemon.registry.create_spawned(
        harness="acme", cwd=str(tmp_path), pid="p-no-credential"
    )
    source = daemon.observer._open_source(participant.id, durable.observer)
    assert isinstance(source, PrimarySource)
    await source.aclose()
    await daemon.aclose()

    hook_only = compile_manifest("acme", hook_manifest(primary=False))
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
async def test_primary_manifest_health_is_reported_without_active_enrichments(
    theater_home, tmp_path, monkeypatch
) -> None:
    manifest = _manifest_with_unavailable_hook()
    harness = compile_manifest("acme", manifest)
    daemon = Daemon(harnesses={"acme": harness})
    monkeypatch.setitem(HARNESSES, "acme", harness)
    monkeypatch.setitem(
        _PLUGINS,
        "acme",
        LoadedPlugin(
            path=tmp_path / "acme",
            source="local",
            name="acme",
            harness=harness,
            manifest=manifest,
        ),
    )
    await daemon.start()
    client = await hook_client(daemon)
    source = None
    try:
        participant = daemon.registry.create_spawned(
            harness="acme", cwd=str(tmp_path), pid="p-primary-health"
        )
        source = daemon.observer._open_source(participant.id, harness.observer)
        assert isinstance(source, PrimarySource)
        daemon.observer._record_channel_health(participant.id, source)

        rows = await client.call("harnesses")
        row = next(row for row in rows if row["name"] == "acme")
        [starting] = row["runtime"]["participants"][0]["channels"]
        assert starting["id"] == "primary"
        assert starting["state"] == "starting"
        assert starting["last_success_at"] is None

        await daemon.observer._read_source(participant.id, source)
        rows = await client.call("harnesses")
        row = next(row for row in rows if row["name"] == "acme")
        [healthy] = row["runtime"]["participants"][0]["channels"]
        assert healthy["state"] == "healthy"
        assert healthy["accepted"] == 0
        assert healthy["last_success_at"] is not None
    finally:
        if source is not None:
            await source.aclose()
        await client.aclose()
        await daemon.aclose()


@pytest.mark.asyncio
async def test_shared_manifest_does_not_crosswire_two_daemon_hook_runtimes(
    theater_home, tmp_path
) -> None:
    harness = compile_manifest("acme", hook_manifest())
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


@pytest.mark.asyncio
async def test_hook_callbacks_stay_off_loop_and_recover_queued_order() -> None:
    correlation_release = threading.Event()
    decode_release = threading.Event()
    runner = HookCallbackRunner(max_in_flight=1, correlation_timeout=30.0, decoder_timeout=0.05)
    runtime = HookRuntime(lambda _participant_id, _channel_id: True, callback_runner=runner)

    def blocking_correlation(_context: HookCorrelationContext) -> str:
        correlation_release.wait(timeout=10.0)
        return "correlated"

    correlation_binding = (
        hook_manifest(correlation=blocking_correlation).observation.hook_channels[0].bindings[0]
    )

    def decoder(context: HookDecodeContext) -> tuple[ChannelFact, ...]:
        if context.native_id == "slow":
            decode_release.wait(timeout=10.0)
        return (
            ChannelFact(
                SignalKind.TOOL,
                TrajectoryFact(kind=TrajectoryKind.TOOL_CALL, native_id=context.native_id),
            ),
        )

    # The loop must serve a timer tick while the callback is still blocked in its
    # worker thread. Ordering proves that without a wall-clock budget: the
    # correlation cannot finish before the release, and a loop blocked by the
    # callback would only resume after the wait timed out, once it is done.
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
    await asyncio.sleep(0)
    assert not correlation.done()
    correlation_release.set()
    assert await correlation == "correlated"

    timed_channel = hook_manifest(max_queue=3, decoder=decoder).observation.hook_channels[0]
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
    # "slow" blocks in the worker and is dropped by the decoder timeout; the
    # straggler still holds the single slot, so the next read reports capacity
    # exhausted and requeues the survivors instead of decoding them.
    assert (await source.read()).error_code == "hook_decode_failed"
    assert (await source.read()).error_code == "hook_decode_failed"
    # Once the straggler finishes, the queued deliveries decode in order. A busy
    # read requeues without yielding, so each retry gives the loop one tick.
    decode_release.set()
    batch = await source.read()
    for _ in range(1000):
        if batch.error_code is None:
            break
        await asyncio.sleep(0)
        batch = await source.read()
    assert [fact.native_id for fact in batch.trajectory] == ["second", "third"]
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

    channel = hook_manifest(decoder=decoder).observation.hook_channels[0]
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
    harness = compile_manifest("acme", hook_manifest(primary=False))
    daemon = Daemon(harnesses={"acme": harness})
    participant = daemon.registry.create_spawned(harness="acme", cwd=None, pid="p-hook-identity")
    plan = install_hook_plan(LaunchPlan(argv=["acme"]), participant, harness.observer)
    record_launch_identity(participant, plan, daemon.registry)
    observer = daemon.observer
    observer._failures._identity_lost.add(participant.id)
    observer._failures._identity_loss_replayed.add(participant.id)
    observer._attachments._receipt_candidates[participant.id] = ("/tmp/transcript", "session")

    same_signal: list[bool] = []

    async def sleep_once(_seconds: float, wake: object = None) -> None:
        same_signal.append(wake is not None and wake is observer.live.wake_signal(participant.id))
        observer._stopping.set()

    observer._sleep = sleep_once
    try:
        # _watch (not _watch_source) owns releasing the watch-created signal.
        await observer._watch(participant.id, "acme")
        assert same_signal == [True]
        assert observer.live._wakeups.keys() == ()
        assert participant.id in observer._failures._identity_lost
        assert participant.id in observer._failures._identity_loss_replayed
        assert participant.id in observer._attachments._receipt_candidates
    finally:
        await daemon.aclose()
