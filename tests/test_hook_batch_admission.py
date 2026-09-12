"""Revalidate hook identity across every await before enrichment assembly."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from theater.harness.channels.composite import CompositeSource, EnrichmentBinding
from theater.harness.channels.hooks import HookRuntime
from theater.harness.contracts.callbacks import HookAdmissionIdentity, HookInstallOverlay
from theater.harness.contracts.channels import (
    ChannelDeclaration,
    ChannelFact,
    ChannelKind,
    HookBinding,
    SignalKind,
)
from theater.harness.contracts.manifest import HookChannelManifest
from theater.harness.contracts.source import Batch, Source
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.trajectory.enums import TrajectoryKind


def _rig(*native_ids):
    identity = HookAdmissionIdentity(harness="example", session_id="session-a")
    current = [identity]
    channel = HookChannelManifest(
        declaration=ChannelDeclaration(id="hooks", kind=ChannelKind.HOOK),
        bindings=(
            HookBinding(
                event="tool",
                signals=(SignalKind.LIFECYCLE,),
                correlation=lambda context: context.payload["id"],
                decoder=lambda context: (
                    ChannelFact(
                        SignalKind.LIFECYCLE,
                        TrajectoryFact(kind=TrajectoryKind.SYSTEM, native_id=context.native_id),
                    ),
                ),
            ),
        ),
        installer=lambda _: HookInstallOverlay(),
    )
    runtime = HookRuntime(lambda *_: True, identity_provider=lambda _: current[0])
    for native_id in native_ids:
        runtime.enqueue(
            participant_id="participant",
            channel=channel,
            event="tool",
            payload={"id": native_id},
            delivery_id=native_id,
            native_id=native_id,
            admission_identity=identity,
        )
    source = runtime.open_source(participant_id="participant", channel=channel)
    return runtime, source, channel, current


@pytest.mark.asyncio
async def test_earlier_decoded_fact_is_invalidated_while_next_decoder_waits(monkeypatch):
    runtime, source, _channel, current = _rig("first", "second")
    entered = asyncio.Event()
    release = asyncio.Event()
    original = runtime._callbacks.decode

    async def delayed_decode(callback, context):
        if context.native_id == "second":
            entered.set()
            await release.wait()
        return await original(callback, context)

    monkeypatch.setattr(runtime._callbacks, "decode", delayed_decode)
    task = asyncio.create_task(source.read())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        current[0] = replace(current[0], session_id="session-b")
        release.set()
        assert (await task).trajectory == ()
    finally:
        release.set()
        await task
        await source.aclose()
        await runtime.aclose()


class _Primary(Source):
    async def read(self):
        return Batch(trajectory=(TrajectoryFact(kind=TrajectoryKind.SYSTEM, native_id="durable"),))


@pytest.mark.asyncio
async def test_completed_hook_batch_is_invalidated_while_sibling_enrichment_waits(monkeypatch):
    runtime, source, channel, current = _rig("old-session-fact")
    decoded = asyncio.Event()
    original = source.read

    async def marked_read():
        batch = await original()
        assert len(batch.trajectory) == 1
        decoded.set()
        return batch

    class RotatingSibling(Source):
        async def read(self):
            await decoded.wait()
            current[0] = replace(current[0], session_id="session-b")
            return Batch()

    monkeypatch.setattr(source, "read", marked_read)
    composite = CompositeSource(
        primary=_Primary(),
        enrichments=(
            EnrichmentBinding(source=source, declaration=channel.declaration),
            EnrichmentBinding(
                source=RotatingSibling(),
                declaration=ChannelDeclaration(id="sibling", kind=ChannelKind.HOOK),
            ),
        ),
    )
    try:
        batch = await composite.read()
        assert [fact.native_id for fact in batch.trajectory] == ["durable"]
    finally:
        await composite.aclose()
        await runtime.aclose()


@pytest.mark.asyncio
async def test_failed_enrichment_revalidation_preserves_durable_primary():
    class BrokenAdmission(Source):
        async def read(self):
            return Batch(trajectory=(TrajectoryFact(kind=TrajectoryKind.SYSTEM),))

        def validate_enrichment_batch(self, batch):
            raise RuntimeError("unavailable admission state")

    source = CompositeSource(
        primary=_Primary(),
        enrichments=(
            EnrichmentBinding(
                source=BrokenAdmission(),
                declaration=ChannelDeclaration(id="broken", kind=ChannelKind.HOOK),
            ),
        ),
    )
    try:
        batch = await source.read()
        assert [fact.native_id for fact in batch.trajectory] == ["durable"]
    finally:
        await source.aclose()
