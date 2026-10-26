"""Hook correlation, retry, semantic dedupe, aggregate bounds and requeue ordering."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Sequence

import pytest

from tests.support.hook_channel_rig import hook_client, hook_event, hook_manifest
from theater.daemon.server import Daemon
from theater.daemon.spawning.planning import (
    install_hook_plan,
    record_launch_identity,
    write_plan_files,
)
from theater.harness.channels import HookRuntime
from theater.harness.channels.hooks import HookDelivery, HookInbox
from theater.harness.channels.hooks.callbacks import HookCallbackRunner
from theater.harness.contracts.callbacks import HookCorrelationContext, HookDecodeContext
from theater.harness.contracts.channels import ChannelFact, SignalKind
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.harness.manifests.compiler import compile_manifest
from theater.protocol import RemoteError
from theater.trajectory.enums import TrajectoryKind


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

    harness = compile_manifest("acme", hook_manifest(correlation=correlation, decoder=decode))
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant = daemon.registry.create_spawned(
        harness="acme", cwd=str(tmp_path), pid="p-correlation"
    )
    plan = install_hook_plan(LaunchPlan(argv=["acme"]), participant, harness.observer)
    record_launch_identity(participant, plan, daemon.registry)
    write_plan_files(plan)
    credential = plan.channel_credentials[0]
    client = await hook_client(daemon)
    try:
        assert await hook_event(
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
        assert await hook_event(
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
            await hook_event(
                client,
                pid=participant.id,
                token=credential.token,
                native_id="explode",
                delivery_id="explode-delivery",
            )
        assert "secret payload detail" not in str(raised.value)
        with pytest.raises(RemoteError, match="correlation is invalid"):
            await hook_event(
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
            release.wait(timeout=10)
            raise RuntimeError("first delivery failed")
        return str(context.payload["native_id"])

    harness = compile_manifest("acme", hook_manifest(correlation=correlation))
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant = daemon.registry.create_spawned(
        harness="acme", cwd=str(tmp_path), pid="p-concurrent-retry"
    )
    plan = install_hook_plan(LaunchPlan(argv=["acme"]), participant, harness.observer)
    record_launch_identity(participant, plan, daemon.registry)
    credential = plan.channel_credentials[0]
    client = await hook_client(daemon)
    try:
        first = asyncio.create_task(
            hook_event(
                client,
                pid=participant.id,
                token=credential.token,
                native_id="same",
                delivery_id="same-delivery",
            )
        )
        assert await asyncio.to_thread(entered.wait, 5)
        second = await hook_event(
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
    harness = compile_manifest("acme", hook_manifest())
    daemon = Daemon(harnesses={"acme": harness})
    await daemon.start()
    participant = daemon.registry.create_spawned(harness="acme", cwd=str(tmp_path), pid="p-dedupe")
    plan = install_hook_plan(LaunchPlan(argv=["acme"]), participant, harness.observer)
    record_launch_identity(participant, plan, daemon.registry)
    write_plan_files(plan)
    credential = plan.channel_credentials[0]
    client = await hook_client(daemon)
    try:
        await hook_event(
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

        await hook_event(
            client,
            pid=participant.id,
            token=credential.token,
            native_id="shared",
            delivery_id="same-fact-new-delivery",
        )
        reopened = daemon.observer._open_source(participant.id, harness.observer)
        assert reopened is not None
        assert (await reopened.read()).trajectory == ()
        await hook_event(
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

    harness = compile_manifest("acme", hook_manifest(correlation=slow_correlation))
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
    client = await hook_client(daemon)
    try:
        before = sum(
            row["kind"] == "agent.harness_event" for row in daemon.store.bus_tail(limit=1000)
        )
        with pytest.raises(RemoteError, match="correlation is invalid"):
            await hook_event(
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

    manifest = hook_manifest(max_queue=2, decoder=decode)
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
    declaration = hook_manifest(max_queue=2).observation.hook_channels[0].declaration
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
