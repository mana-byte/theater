"""Available hooks deliver each declared event and correlated tool facts through the stock CLI.

No scenario exists for an available OTel channel; it is explicitly inconclusive.

The event oracle joins the hook bus's admitted native id to its projected trajectory fact.
"""

from __future__ import annotations

import shlex
import time
import uuid

import pytest

from harness_compat.contract import MockReply, MockToolCall
from harness_compat.support.channel_oracles import capture_channel_facts, expect_channel_facts
from harness_compat.support.live_rig import inconclusive
from harness_compat.support.tool_effects import check_tool_fit, expect_runs, mcp_effect
from theater.harness.contracts.manifest import HookChannelManifest, OtelChannelManifest

pytestmark = [pytest.mark.integration, pytest.mark.family("channels")]


async def test_a_channel_delivers_its_declared_facts(live, obligation):
    channel_id, _, event = obligation.detail.partition("/")
    channel = next(
        item
        for item in live.harness.observer.enrichment_manifests()
        if isinstance(item, (HookChannelManifest, OtelChannelManifest))
        and item.declaration.id == channel_id
    )
    if isinstance(channel, OtelChannelManifest):
        inconclusive("no scenario exists for an available OTel channel")
    assert isinstance(channel, HookChannelManifest)
    approval = next(
        value
        for value in ("yolo", "edits", "manual")
        if value in live.harness.launch_parameter_support.approvals
    )
    participant = await live.spawn(approval)
    await live.ready(participant)
    # First-turn hooks can precede durable transcript identity; prove ingress after binding.
    warmup = f"CHANNEL-READY-{uuid.uuid4().hex}"
    live.queue(MockReply(warmup))
    warmed = await live.send(participant, warmup)
    assert warmed.state == "done" and warmup in str(warmed.result), warmed.error
    await live.until(
        participant,
        lambda item: (
            item.transcript_identity is not None
            and item.transcript_identity.state == "trusted"
            and item.transcript_identity.location is not None
        ),
        what="bound a trusted transcript",
    )
    await live.ready(participant)
    if event:
        assert obligation.detail in live.compat.hook_events, (
            f"the adapter has no stimulus for declared hook {obligation.detail}"
        )
        probe = live.compat.hook_events[obligation.detail]
        release = live.workdir / f".hook-release-{uuid.uuid4().hex}"
        call_id = None if probe is None else f"hook_probe_{uuid.uuid4().hex}"
        call = (
            MockToolCall(probe.name, probe.render(release=shlex.quote(str(release))), call_id)
            if probe is not None
            else None
        )
        final = f"HOOK-{uuid.uuid4().hex}"
        if call is not None:
            live.queue(MockReply("hook probe", tool_calls=(call,)))
        live.queue(MockReply(final))
        prompt = f"Run the scripted probe, then reply {final}"

        async def check_recipe():
            if call is not None:
                check_tool_fit(live, prompt, call)

        async with capture_channel_facts(live.client, participant) as page:
            turn_started_at = time.time()
            handle = await live.submit(participant, prompt)
            try:
                await expect_channel_facts(
                    live.client,
                    participant,
                    channel,
                    page,
                    call_id,
                    event=event,
                    turn_started_at=turn_started_at,
                    evidence=live.session.evidence_dir / f"{live.label}.channels.json",
                    guard=check_recipe,
                )
            finally:
                if probe is not None and "release" in probe.placeholders:
                    release.touch()
            job = await live.job(handle, guard=check_recipe)
            await check_recipe()
            assert job.state == "done" and final in str(job.result), job.error
        return
    effect = await mcp_effect(live, participant)
    # The mock numbers calls within each reply; this turn scripts exactly one.
    call_id = "toolu_mock_0" if live.protocol == "anthropic" else "call_mock_0"

    async with capture_channel_facts(live.client, participant) as page:
        turn_started_at = time.time()
        await expect_runs(live, participant, effect)
        await expect_channel_facts(
            live.client,
            participant,
            channel,
            page,
            call_id,
            turn_started_at=turn_started_at,
            evidence=live.session.evidence_dir / f"{live.label}.channels.json",
        )
