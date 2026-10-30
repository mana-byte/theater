"""Exact hook obligations cannot pass from another event, stale delivery or unrelated projection."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from harness_compat.support.channel_oracles import (
    _event_projected,
    expect_channel_facts,
)
from tests.support.claude_hook_rig import captured_hooks
from tests.support.hook_channel_rig import hook_manifest
from theater.daemon.trajectory.project import fact_to_record
from theater.frontend.trajectory import TrajectoryKind
from theater.harness.builtin.plugins.claude.observation import OBSERVATION
from theater.harness.contracts.callbacks import HookCorrelationContext, HookDecodeContext
from theater.trajectory.identity import namespaced_native_id


def record(value, native_id, *, participant="p", source="hook", call_id=None, omitted=0):
    return SimpleNamespace(
        participant_id=participant,
        source=source,
        source_epoch="epoch",
        kind=TrajectoryKind.SYSTEM,
        native_id=namespaced_native_id(native_id, "epoch"),
        call_id=call_id,
        details=(SimpleNamespace(preview=SimpleNamespace(text=value, omitted_bytes=omitted)),),
    )


def claude_hook_context(payload, event):
    channel = OBSERVATION.hook_channels[0]
    binding = next(binding for binding in channel.bindings if binding.event == event)
    native_id = binding.correlation(
        HookCorrelationContext(
            participant_id="p",
            channel_id=channel.declaration.id,
            event=event,
            payload=payload,
            expected_session_id=payload["session_id"],
            expected_transcript_location=payload["transcript_path"],
            expected_session_provenance="exact",
        )
    )
    return binding, HookDecodeContext(
        participant_id="p",
        channel_id=channel.declaration.id,
        event=event,
        payload=payload,
        native_id=native_id,
    )


@pytest.mark.parametrize("event", ["prompt.submit", "turn.stop"])
@pytest.mark.parametrize("failure", ["other-event", "dropped", "stale"])
async def test_each_hook_requires_its_own_new_accepted_delivery(tmp_path, event, failure):
    channel = hook_manifest().observation.hook_channels[0]
    channel = replace(
        channel,
        bindings=tuple(
            replace(channel.bindings[0], event=name)
            for name in ("tool.finished", "prompt.submit", "turn.stop")
        ),
    )
    row = {
        "id": 1,
        "ts": 200.0,
        "to_id": "p",
        "payload": {
            "channel": "native-hooks",
            "event": "tool.finished" if failure == "other-event" else event,
            "native_id": "admitted",
            "dropped": failure == "dropped",
        },
    }
    if failure == "stale":
        row["ts"] = 50.0

    async def bus_tail(**_kwargs):
        return SimpleNamespace(value=SimpleNamespace(items=[row]))

    client = SimpleNamespace(diagnostics=SimpleNamespace(bus_tail=bus_tail))
    page = SimpleNamespace(stream_id="s", cursor="0", records=())
    with pytest.raises(AssertionError, match="no accepted event"):
        await expect_channel_facts(
            client,
            "p",
            channel,
            page,
            None,
            event=event,
            turn_started_at=100.0,
            evidence=tmp_path / "channel.json",
            seconds=0,
        )


def test_event_projection_matches_the_admitted_native_id_and_complete_value():
    accepted = ({"native_id": "admitted"},)
    assert _event_projected([record("turn.stop", "admitted")], accepted, "p", "turn.stop")
    primary = record("turn.stop", "admitted", source="transcript")
    primary.kind = TrajectoryKind.ASSISTANT
    assert not _event_projected([primary], accepted, "p", "turn.stop")
    for wrong in (
        record("turn.stop", "another-invocation"),
        record("prompt.submit", "admitted"),
        record("turn.stop", "admitted", participant="other"),
        record("turn.stop", "admitted", omitted=1),
        record("other-event", "admitted", call_id="turn.stop"),
    ):
        assert not _event_projected([wrong], accepted, "p", "turn.stop")


@pytest.mark.parametrize(
    ("fixture_key", "event"),
    [
        ("pre_tool_use", "PreToolUse"),
        ("post_tool_use", "PostToolUse"),
        ("post_tool_use_failure", "PostToolUseFailure"),
    ],
)
@pytest.mark.parametrize("source_epoch", ["hook-epoch", "e" * 500])
def test_oracle_accepts_real_claude_projections_including_bounded_ids(
    fixture_key, event, source_epoch
):
    payload = captured_hooks()[fixture_key]
    binding, context = claude_hook_context(payload, event)
    [decoded] = binding.decoder(context)
    native_id = context.native_id
    projected = fact_to_record(decoded.fact, participant_id="p", source_epoch=source_epoch)
    assert projected.native_id != native_id
    assert _event_projected([projected], ({"native_id": native_id},), "p", event)
    assert not _event_projected(
        [replace(projected, source_epoch="other")],
        ({"native_id": native_id},),
        "p",
        event,
    )


def test_rejected_failure_decode_cannot_qualify_from_the_same_tools_start():
    records = []
    accepted = []
    identity = captured_hooks()["pre_tool_use"]
    for fixture, event in (
        ("pre_tool_use", "PreToolUse"),
        ("post_tool_use_failure", "PostToolUseFailure"),
    ):
        payload = dict(captured_hooks()[fixture])
        payload.update({key: identity[key] for key in ("session_id", "transcript_path")})
        payload["tool_use_id"] = "same-tool"
        payload.pop("error", None)
        binding, context = claude_hook_context(payload, event)
        accepted.append({"native_id": context.native_id})
        if event == "PreToolUse":
            [decoded] = binding.decoder(context)
            records.append(fact_to_record(decoded.fact, participant_id="p", source_epoch="epoch"))
        else:
            with pytest.raises(ValueError, match="error"):
                binding.decoder(context)
    assert accepted[0]["native_id"] == accepted[1]["native_id"]
    assert _event_projected(records, accepted, "p", "PreToolUse")
    assert not _event_projected(records, accepted, "p", "PostToolUseFailure")
