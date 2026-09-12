"""Focused Claude Code native-hook observation coverage."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
from pathlib import Path

import pytest

from theater import paths
from theater.client import DaemonClient
from theater.daemon.server import Daemon
from theater.daemon.spawning.planning import install_hook_plan, record_launch_identity
from theater.daemon.trajectory.history import source_epoch_for
from theater.daemon.trajectory.project import project_batch
from theater.harness.builtin.plugins.claude.hooks import (
    NATIVE_HOOK_CHANNEL,
    correlate_tool_hook,
    decode_tool_hook,
    install_native_hooks,
    native_hook_settings,
    native_hook_token_path,
)
from theater.harness.builtin.plugins.claude.launch import plan_launch
from theater.harness.builtin.plugins.claude.manifest import MANIFEST
from theater.harness.builtin.plugins.claude.observation import OBSERVATION
from theater.harness.channels.hooks import HookRuntime
from theater.harness.contracts.callbacks import (
    HookCorrelationContext,
    HookDecodeContext,
    HookInstallContext,
    LaunchContext,
)
from theater.harness.contracts.channels import ChannelKind, SignalKind, SignalOwnership
from theater.harness.manifests.compiler import compile_manifest
from theater.models import Participant
from theater.protocol import RemoteError
from theater.trajectory.enums import TrajectoryFailureCategory, TrajectoryKind, TrajectoryStatus
from theater.transcript_identity import canonical_location

_FIXTURE = Path(__file__).parent / "fixtures" / "claude_hooks" / "captured_2.1.220.json"


def _captured() -> dict:
    return json.loads(_FIXTURE.read_text())


def _binding(event: str):
    channel = OBSERVATION.hook_channels[0]
    return next(binding for binding in channel.bindings if binding.event == event)


def _trusted_identity(payload: dict[str, object]) -> dict[str, str]:
    session_id = payload.get("session_id")
    transcript_path = payload.get("transcript_path")
    assert isinstance(session_id, str)
    assert isinstance(transcript_path, str)
    return {
        "expected_session_id": session_id,
        "expected_transcript_location": canonical_location(transcript_path),
        "expected_session_provenance": "exact",
    }


def _correlation_context(
    payload: dict[str, object],
    event: str,
    *,
    participant_id: str = "participant",
    delivery_id: str | None = None,
) -> HookCorrelationContext:
    return HookCorrelationContext(
        participant_id=participant_id,
        channel_id=NATIVE_HOOK_CHANNEL,
        event=event,
        payload=payload,
        delivery_id=delivery_id,
        **_trusted_identity(payload),
    )


@pytest.mark.parametrize(
    ("fixture_key", "event", "status", "revision"),
    (
        ("pre_tool_use", "PreToolUse", TrajectoryStatus.RUNNING, 0),
        ("post_tool_use", "PostToolUse", TrajectoryStatus.COMPLETED, 1),
        ("post_tool_use_failure", "PostToolUseFailure", TrajectoryStatus.ERROR, 1),
    ),
)
def test_captured_tool_hooks_decode_as_non_authoritative_lifecycle(
    fixture_key, event, status, revision
) -> None:
    payload = _captured()[fixture_key]
    assert isinstance(payload, dict)
    binding = _binding(event)
    native_id = binding.correlation(_correlation_context(payload, event))

    [decoded] = binding.decoder(
        HookDecodeContext(
            participant_id="participant",
            channel_id=NATIVE_HOOK_CHANNEL,
            event=event,
            payload=payload,
            native_id=native_id,
        )
    )

    fact = decoded.fact
    assert decoded.signal is SignalKind.LIFECYCLE
    assert fact.kind is TrajectoryKind.SYSTEM
    assert fact.status is status
    assert fact.revision == revision
    assert fact.native_id == native_id
    assert fact.source == "claude-hook"
    assert all(detail.name not in {"tool_input", "tool_response"} for detail in fact.details)
    details = {detail.name: detail.preview.text for detail in fact.details}
    assert details["session_id"] == payload["session_id"]
    assert details["tool_use_id"] == payload["tool_use_id"]
    if event == "PostToolUse":
        assert fact.timing is not None
        assert fact.timing.duration_ms == payload["duration_ms"]
    if event == "PostToolUseFailure":
        assert fact.failure is not None
        assert fact.failure.category is TrajectoryFailureCategory.TOOL
        assert fact.failure.detail == payload["error"]


def test_hook_schema_rejects_invalid_identity_and_malformed_input() -> None:
    payload = _captured()["pre_tool_use"]
    assert isinstance(payload, dict)

    missing_session = dict(payload)
    missing_session.pop("session_id")
    with pytest.raises(ValueError, match="session_id"):
        correlate_tool_hook(
            HookCorrelationContext(
                participant_id="participant",
                channel_id=NATIVE_HOOK_CHANNEL,
                event="PreToolUse",
                payload=missing_session,
                **_trusted_identity(payload),
            )
        )

    mismatched_event = dict(payload, hook_event_name="PostToolUse")
    with pytest.raises(ValueError, match="hook_event_name"):
        correlate_tool_hook(
            HookCorrelationContext(
                participant_id="participant",
                channel_id=NATIVE_HOOK_CHANNEL,
                event="PreToolUse",
                payload=mismatched_event,
                **_trusted_identity(payload),
            )
        )

    mismatched_session = dict(payload, session_id="other-session")
    with pytest.raises(ValueError, match="does not match session_id"):
        correlate_tool_hook(
            HookCorrelationContext(
                participant_id="participant",
                channel_id=NATIVE_HOOK_CHANNEL,
                event="PreToolUse",
                payload=mismatched_session,
                **_trusted_identity(payload),
            )
        )

    native_id = correlate_tool_hook(_correlation_context(payload, "PreToolUse"))

    malformed_tool_input = dict(payload, tool_input=[])
    with pytest.raises(TypeError, match="tool_input"):
        decode_tool_hook(
            HookDecodeContext(
                participant_id="participant",
                channel_id=NATIVE_HOOK_CHANNEL,
                event="PreToolUse",
                payload=malformed_tool_input,
                native_id=native_id,
            )
        )
    with pytest.raises(ValueError, match="native identity"):
        decode_tool_hook(
            HookDecodeContext(
                participant_id="participant",
                channel_id=NATIVE_HOOK_CHANNEL,
                event="PreToolUse",
                payload=payload,
                native_id=native_id + "-wrong",
            )
        )


def test_hook_schema_requires_a_current_trusted_identity() -> None:
    payload = _captured()["pre_tool_use"]
    assert isinstance(payload, dict)

    with pytest.raises(ValueError, match="trusted transcript identity"):
        correlate_tool_hook(
            HookCorrelationContext(
                participant_id="participant",
                channel_id=NATIVE_HOOK_CHANNEL,
                event="PreToolUse",
                payload=payload,
            )
        )
    with pytest.raises(ValueError, match="trusted transcript identity"):
        correlate_tool_hook(
            HookCorrelationContext(
                participant_id="participant",
                channel_id=NATIVE_HOOK_CHANNEL,
                event="PreToolUse",
                payload=payload,
                expected_session_id=payload["session_id"],
                expected_transcript_location=canonical_location(payload["transcript_path"]),
                expected_session_provenance="heuristic",
            )
        )
    with pytest.raises(ValueError, match="quarantined"):
        correlate_tool_hook(
            HookCorrelationContext(
                participant_id="participant",
                channel_id=NATIVE_HOOK_CHANNEL,
                event="PreToolUse",
                payload=payload,
                identity_lost=True,
                **_trusted_identity(payload),
            )
        )


def test_hook_schema_rejects_consistently_changed_untrusted_session_and_transcript() -> None:
    payload = _captured()["pre_tool_use"]
    assert isinstance(payload, dict)
    other_session = "33333333-3333-4333-8333-333333333333"
    changed = dict(
        payload,
        session_id=other_session,
        transcript_path=str(Path(payload["transcript_path"]).with_name(f"{other_session}.jsonl")),
    )

    with pytest.raises(ValueError, match="trusted session"):
        correlate_tool_hook(
            HookCorrelationContext(
                participant_id="participant",
                channel_id=NATIVE_HOOK_CHANNEL,
                event="PreToolUse",
                payload=changed,
                **_trusted_identity(payload),
            )
        )


def test_interrupted_tool_failure_is_lifecycle_only_not_completion_evidence() -> None:
    payload = dict(_captured()["post_tool_use_failure"], is_interrupt=True)
    binding = _binding("PostToolUseFailure")
    native_id = binding.correlation(_correlation_context(payload, "PostToolUseFailure"))

    [decoded] = binding.decoder(
        HookDecodeContext(
            participant_id="participant",
            channel_id=NATIVE_HOOK_CHANNEL,
            event="PostToolUseFailure",
            payload=payload,
            native_id=native_id,
        )
    )

    assert decoded.signal is SignalKind.LIFECYCLE
    assert decoded.fact.status is TrajectoryStatus.INTERRUPTED
    assert decoded.fact.failure is None
    assert decoded.fact.kind is TrajectoryKind.SYSTEM


def test_absent_hook_credential_leaves_durable_transcript_primary() -> None:
    runtime = HookRuntime(lambda _participant_id, _channel_id: False)
    try:
        assert not runtime.has_active("participant", OBSERVATION.enrichments)
    finally:
        # This direct unit probe does not start any daemon or native process.
        import asyncio

        asyncio.run(runtime.aclose())

    assert OBSERVATION.primary is not None
    primary = {(cap.signal, cap.ownership) for cap in OBSERVATION.primary.channel.capabilities}
    hook_channel = OBSERVATION.hook_channels[0].declaration
    hooks = {(cap.signal, cap.ownership) for cap in hook_channel.capabilities}
    assert (SignalKind.TURN, SignalOwnership.PRIMARY) in primary
    assert (SignalKind.USAGE, SignalOwnership.PRIMARY) in primary
    assert hooks == {(SignalKind.LIFECYCLE, SignalOwnership.ENRICHMENT)}


@pytest.mark.asyncio
async def test_reordered_and_duplicate_lifecycle_hooks_settle_to_one_terminal_observation() -> None:
    captured = _captured()
    pre = captured["pre_tool_use"]
    post = captured["post_tool_use"]
    assert isinstance(pre, dict)
    assert isinstance(post, dict)
    channel = OBSERVATION.hook_channels[0]
    runtime = HookRuntime(lambda _participant_id, _channel_id: True)
    source = None
    try:
        # Async Claude hooks have no native delivery id.  The same native
        # session/tool identity makes a duplicate PreToolUse harmless, while a
        # late PreToolUse cannot overwrite an already-observed terminal state.
        for event, payload, delivery_id in (
            ("PostToolUse", post, "post-first"),
            ("PreToolUse", pre, "pre-late"),
            ("PreToolUse", pre, "pre-duplicate"),
        ):
            native_id = correlate_tool_hook(
                _correlation_context(payload, event, delivery_id=delivery_id)
            )
            runtime.enqueue(
                participant_id="participant",
                channel=channel,
                event=event,
                payload=payload,
                delivery_id=delivery_id,
                native_id=native_id,
            )
        source = runtime.open_source(participant_id="participant", channel=channel)
        batch = await source.read()
        assert [(fact.status, fact.revision) for fact in batch.trajectory] == [
            (TrajectoryStatus.COMPLETED, 1),
            (TrajectoryStatus.RUNNING, 0),
        ]

        projected = project_batch(batch, participant_id="participant", source_epoch="session")
        assert len(projected) == 1
        [terminal] = projected
        assert terminal.kind is TrajectoryKind.SYSTEM
        assert terminal.status is TrajectoryStatus.COMPLETED
        assert terminal.revision == 1
    finally:
        if source is not None:
            await source.aclose()
        await runtime.aclose()


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
    payload = _captured()["pre_tool_use"]
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


def test_launch_composes_receipt_and_async_native_hook_settings(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    participant_id = "claude-hooks"
    plan = plan_launch(
        LaunchContext(
            participant_id=participant_id,
            prompt="",
            config_path=tmp_path / "mcp.json",
            approval="manual",
        )
    )
    settings_path = next(path for path in plan.files if path.name == "claude.settings.json")
    native = native_hook_settings(participant_id)["hooks"]
    assert not set(native).intersection(json.loads(plan.files[settings_path])["hooks"])
    harness = compile_manifest("claude", MANIFEST)
    installed = install_hook_plan(
        plan,
        Participant(id=participant_id, harness="claude"),
        harness.observer,
        enabled_channels=frozenset({NATIVE_HOOK_CHANNEL}),
    )
    settings = json.loads(installed.files[settings_path])
    assert isinstance(native, dict)
    for event, entries in native.items():
        assert settings["hooks"][event] == entries
        [entry] = entries
        [hook] = entry["hooks"]
        assert hook["type"] == "command"
        assert hook["async"] is True
        assert "--strict-exit" not in hook["command"]
        assert shlex.split(hook["command"])[1:3] == ["harness-event", event]

    [credential] = installed.channel_credentials
    assert credential.kind is ChannelKind.HOOK
    assert credential.channel_id == NATIVE_HOOK_CHANNEL
    assert credential.token_path == native_hook_token_path(participant_id)
    assert credential.token_path == (
        paths.participant_observation_dir(participant_id, "claude") / "hook-native-hooks.token"
    )


def test_native_hook_installer_refuses_a_token_path_other_than_its_command_path(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    with pytest.raises(ValueError, match="does not match"):
        install_native_hooks(
            HookInstallContext(
                participant_id="claude-hooks",
                channel_id=NATIVE_HOOK_CHANNEL,
                token_file=tmp_path / "wrong-token",
                theater_executable="theater",
            )
        )
