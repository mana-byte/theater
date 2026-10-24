"""Claude hook identity is revalidated across every await before enrichment and delivery."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from pathlib import Path

import pytest

from tests.support.claude_hook_rig import captured_hooks
from theater.client import DaemonClient
from theater.daemon.server import Daemon
from theater.daemon.spawning.planning import install_hook_plan, record_launch_identity
from theater.daemon.trajectory.history import source_epoch_for
from theater.daemon.trajectory.project import project_batch
from theater.harness.builtin.plugins.claude.hooks import (
    NATIVE_HOOK_CHANNEL,
)
from theater.harness.builtin.plugins.claude.launch import plan_launch
from theater.harness.builtin.plugins.claude.manifest import MANIFEST
from theater.harness.builtin.plugins.claude.observation import OBSERVATION
from theater.harness.channels.composite import CompositeSource, EnrichmentBinding
from theater.harness.channels.hooks import HookRuntime
from theater.harness.contracts.callbacks import (
    HookAdmissionIdentity,
    HookCorrelationContext,
    HookInstallOverlay,
    LaunchContext,
)
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
from theater.harness.manifests.compiler import compile_manifest
from theater.protocol import RemoteError
from theater.trajectory.enums import TrajectoryKind
from theater.transcript_identity import canonical_location

pytestmark = pytest.mark.usefixtures("available_harness_binaries")


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


async def _claude_hook_rig(tmp_path):
    harness = compile_manifest("claude", MANIFEST)
    daemon = Daemon(harnesses={"claude": harness})
    await daemon.start()
    participant = daemon.registry.create_spawned(
        harness="claude", cwd=str(tmp_path), pid="p-claude-hook-rpc"
    )
    plan = install_hook_plan(
        plan_launch(
            LaunchContext(
                participant_id=participant.id,
                prompt="",
                config_path=tmp_path / "mcp.json",
                approval="manual",
            )
        ),
        participant,
        harness.observer,
        enabled_channels=frozenset({NATIVE_HOOK_CHANNEL}),
    )
    record_launch_identity(participant, plan, daemon.registry)
    [credential] = plan.channel_credentials
    return daemon, participant, credential.token


async def _claude_hook_event(
    client: DaemonClient,
    *,
    participant_id: str,
    token: str,
    payload: dict[str, object],
) -> dict:
    return await client.call(
        "harness.event",
        id=participant_id,
        token=token,
        channel=NATIVE_HOOK_CHANNEL,
        event="PreToolUse",
        payload=payload,
    )


def _pre_tool_payload(session_id: str, transcript_path: Path) -> dict[str, object]:
    payload = captured_hooks()["pre_tool_use"]
    assert isinstance(payload, dict)
    return dict(payload, session_id=session_id, transcript_path=str(transcript_path))


def _harness_event_count(daemon: Daemon) -> int:
    return sum(row["kind"] == "agent.harness_event" for row in daemon.store.bus_tail(limit=1000))


@pytest.mark.asyncio
async def test_claude_hook_rpc_rejects_other_session_and_transcript_identity(
    theater_home, tmp_path
) -> None:
    daemon, participant, token = await _claude_hook_rig(tmp_path)
    client = DaemonClient(autostart=False)
    await client.connect()
    try:
        session_id = "11111111-1111-4111-8111-111111111111"
        transcript = tmp_path / "trusted" / f"{session_id}.jsonl"
        daemon.store.record_transcript_receipt(
            participant.id,
            session_id=session_id,
            transcript_location=str(transcript),
        )
        other_session = "22222222-2222-4222-8222-222222222222"
        other_transcript = tmp_path / "other" / f"{other_session}.jsonl"
        with pytest.raises(RemoteError, match="correlation is invalid"):
            await _claude_hook_event(
                client,
                participant_id=participant.id,
                token=token,
                payload=_pre_tool_payload(other_session, other_transcript),
            )

        # The filename/session join alone is insufficient: a same-session
        # path from another location must also fail the daemon-trusted join.
        with pytest.raises(RemoteError, match="correlation is invalid"):
            await _claude_hook_event(
                client,
                participant_id=participant.id,
                token=token,
                payload=_pre_tool_payload(session_id, tmp_path / "other" / transcript.name),
            )
        assert _harness_event_count(daemon) == 0
        assert daemon.hook_runtime.health_snapshot(participant.id) == ()
    finally:
        await client.aclose()
        await daemon.aclose()


@pytest.mark.asyncio
async def test_claude_hook_rpc_rechecks_identity_after_async_correlation(  # noqa: PLR0915
    theater_home, tmp_path, monkeypatch
) -> None:
    daemon, participant, token = await _claude_hook_rig(tmp_path)
    client = DaemonClient(autostart=False)
    await client.connect()
    entered = asyncio.Event()
    release = asyncio.Event()
    contexts: list[HookCorrelationContext] = []
    original_correlate = daemon.hook_runtime.correlate

    async def delayed_correlation(binding, context):
        contexts.append(context)
        entered.set()
        await release.wait()
        return await original_correlate(binding, context)

    monkeypatch.setattr(daemon.hook_runtime, "correlate", delayed_correlation)
    task = None
    try:
        old_session = "11111111-1111-4111-8111-111111111111"
        old_transcript = tmp_path / "old" / f"{old_session}.jsonl"
        daemon.store.record_transcript_receipt(
            participant.id,
            session_id=old_session,
            transcript_location=str(old_transcript),
        )
        old_payload = _pre_tool_payload(old_session, old_transcript)
        task = asyncio.create_task(
            _claude_hook_event(
                client,
                participant_id=participant.id,
                token=token,
                payload=old_payload,
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        [context] = contexts
        assert context.expected_session_id == old_session
        assert context.expected_transcript_location == str(old_transcript)
        assert context.expected_session_provenance == "exact"
        assert context.identity_lost is False

        new_session = "22222222-2222-4222-8222-222222222222"
        new_transcript = tmp_path / "new" / f"{new_session}.jsonl"
        daemon.store.record_transcript_receipt(
            participant.id,
            session_id=new_session,
            transcript_location=str(new_transcript),
        )
        release.set()
        with pytest.raises(RemoteError, match="identity changed during correlation"):
            await task
        task = None
        monkeypatch.setattr(daemon.hook_runtime, "correlate", original_correlate)

        # A delivery arriving after rotation is admitted against the new
        # snapshot and cannot be enqueued under the new source epoch.
        with pytest.raises(RemoteError, match="correlation is invalid"):
            await _claude_hook_event(
                client,
                participant_id=participant.id,
                token=token,
                payload=old_payload,
            )
        assert _harness_event_count(daemon) == 0

        accepted = await _claude_hook_event(
            client,
            participant_id=participant.id,
            token=token,
            payload=_pre_tool_payload(new_session, new_transcript),
        )
        assert accepted == {"ok": True, "duplicate": False, "dropped": False}
        assert _harness_event_count(daemon) == 1

        source = daemon.hook_runtime.open_source(
            participant_id=participant.id, channel=OBSERVATION.hook_channels[0]
        )
        try:
            [fact] = (await source.read()).trajectory
            details = {detail.name: detail.preview.text for detail in fact.details}
            assert details["session_id"] == new_session
            assert details["transcript_path"] == canonical_location(str(new_transcript))
        finally:
            await source.aclose()
    finally:
        release.set()
        if task is not None:
            with contextlib.suppress(RemoteError):
                await task
        await client.aclose()
        await daemon.aclose()


@pytest.mark.asyncio
async def test_claude_hook_queue_drops_admitted_delivery_after_identity_rotation(
    theater_home, tmp_path
) -> None:
    """An A delivery queued before rotation must never project in B's epoch."""
    daemon, participant, token = await _claude_hook_rig(tmp_path)
    client = DaemonClient(autostart=False)
    await client.connect()
    source = None
    try:
        old_session = "11111111-1111-4111-8111-111111111111"
        old_transcript = tmp_path / "old" / f"{old_session}.jsonl"
        daemon.store.record_transcript_receipt(
            participant.id,
            session_id=old_session,
            transcript_location=str(old_transcript),
        )
        assert await _claude_hook_event(
            client,
            participant_id=participant.id,
            token=token,
            payload=_pre_tool_payload(old_session, old_transcript),
        ) == {"ok": True, "duplicate": False, "dropped": False}

        new_session = "22222222-2222-4222-8222-222222222222"
        new_transcript = tmp_path / "new" / f"{new_session}.jsonl"
        daemon.store.record_transcript_receipt(
            participant.id,
            session_id=new_session,
            transcript_location=str(new_transcript),
        )

        source = daemon.hook_runtime.open_source(
            participant_id=participant.id, channel=OBSERVATION.hook_channels[0]
        )
        batch = await source.read()
        current = daemon.store.get_participant(participant.id)
        assert current is not None and current.session_id == new_session
        assert batch.trajectory == ()
        assert (
            project_batch(
                batch,
                participant_id=participant.id,
                source_epoch=source_epoch_for(current, None),
            )
            == ()
        )
        health = source.channel_health()
        assert health is not None
        assert health.dropped == 1
        assert "hook admission identity changed" in health.diagnostics
    finally:
        if source is not None:
            await source.aclose()
        await client.aclose()
        await daemon.aclose()


@pytest.mark.asyncio
async def test_claude_hook_queue_drops_when_identity_rotates_during_decode(
    theater_home, tmp_path, monkeypatch
) -> None:
    """The source rechecks identity after its bounded off-loop decoder await."""
    daemon, participant, token = await _claude_hook_rig(tmp_path)
    client = DaemonClient(autostart=False)
    await client.connect()
    entered = asyncio.Event()
    release = asyncio.Event()
    source = None
    read_task = None
    original_decode = daemon.hook_runtime._callbacks.decode

    async def delayed_decode(callback, context):
        entered.set()
        await release.wait()
        return await original_decode(callback, context)

    monkeypatch.setattr(daemon.hook_runtime._callbacks, "decode", delayed_decode)
    try:
        old_session = "11111111-1111-4111-8111-111111111111"
        old_transcript = tmp_path / "old" / f"{old_session}.jsonl"
        daemon.store.record_transcript_receipt(
            participant.id,
            session_id=old_session,
            transcript_location=str(old_transcript),
        )
        assert await _claude_hook_event(
            client,
            participant_id=participant.id,
            token=token,
            payload=_pre_tool_payload(old_session, old_transcript),
        ) == {"ok": True, "duplicate": False, "dropped": False}
        source = daemon.hook_runtime.open_source(
            participant_id=participant.id, channel=OBSERVATION.hook_channels[0]
        )
        read_task = asyncio.create_task(source.read())
        await asyncio.wait_for(entered.wait(), timeout=1)

        new_session = "22222222-2222-4222-8222-222222222222"
        new_transcript = tmp_path / "new" / f"{new_session}.jsonl"
        daemon.store.record_transcript_receipt(
            participant.id,
            session_id=new_session,
            transcript_location=str(new_transcript),
        )
        release.set()
        batch = await read_task
        read_task = None
        assert batch.trajectory == ()
        health = source.channel_health()
        assert health is not None
        assert health.dropped == 1
        assert "hook admission identity changed" in health.diagnostics
    finally:
        release.set()
        if read_task is not None:
            await read_task
        if source is not None:
            await source.aclose()
        await client.aclose()
        await daemon.aclose()
