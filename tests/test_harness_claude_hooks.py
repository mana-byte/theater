"""Focused Claude Code native-hook observation coverage."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from theater import paths
from theater.daemon.spawning.planning import install_hook_plan
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
from theater.trajectory.enums import TrajectoryFailureCategory, TrajectoryKind, TrajectoryStatus

_FIXTURE = Path(__file__).parent / "fixtures" / "claude_hooks" / "captured_2.1.220.json"


def _captured() -> dict:
    return json.loads(_FIXTURE.read_text())


def _binding(event: str):
    channel = OBSERVATION.hook_channels[0]
    return next(binding for binding in channel.bindings if binding.event == event)


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
    native_id = binding.correlation(
        HookCorrelationContext(
            participant_id="participant",
            channel_id=NATIVE_HOOK_CHANNEL,
            event=event,
            payload=payload,
        )
    )

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
            )
        )

    native_id = correlate_tool_hook(
        HookCorrelationContext(
            participant_id="participant",
            channel_id=NATIVE_HOOK_CHANNEL,
            event="PreToolUse",
            payload=payload,
        )
    )

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


def test_interrupted_tool_failure_is_lifecycle_only_not_completion_evidence() -> None:
    payload = dict(_captured()["post_tool_use_failure"], is_interrupt=True)
    binding = _binding("PostToolUseFailure")
    native_id = binding.correlation(
        HookCorrelationContext(
            participant_id="participant",
            channel_id=NATIVE_HOOK_CHANNEL,
            event="PostToolUseFailure",
            payload=payload,
        )
    )

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
                HookCorrelationContext(
                    participant_id="participant",
                    channel_id=NATIVE_HOOK_CHANNEL,
                    event=event,
                    payload=payload,
                    delivery_id=delivery_id,
                )
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
    settings = json.loads(plan.files[settings_path])
    native = native_hook_settings(participant_id)["hooks"]
    assert isinstance(native, dict)
    for event, entries in native.items():
        assert settings["hooks"][event] == entries
        [entry] = entries
        [hook] = entry["hooks"]
        assert hook["type"] == "command"
        assert hook["async"] is True
        assert "--strict-exit" not in hook["command"]
        assert shlex.split(hook["command"])[1:3] == ["harness-event", event]

    harness = compile_manifest("claude", MANIFEST)
    installed = install_hook_plan(
        plan,
        Participant(id=participant_id, harness="claude"),
        harness.observer,
    )
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
