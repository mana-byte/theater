"""Available hooks deliver correlated tool facts through the stock CLI.

No scenario exists for an available OTel channel; it is explicitly inconclusive.

With two enrichment channels on one harness, a distinct source cannot attribute a fact to
one channel. An exact join would require the hook bus row to carry its admitted native id.
"""

from __future__ import annotations

import time

import pytest

from harness_compat.support.channel_oracles import capture_channel_facts, expect_channel_tool_facts
from harness_compat.support.live_rig import inconclusive
from harness_compat.support.tool_effects import expect_runs, mcp_effect
from theater.harness.contracts.manifest import HookChannelManifest, OtelChannelManifest

pytestmark = [pytest.mark.integration, pytest.mark.family("channels")]


async def test_a_channel_delivers_correlated_tool_facts(live, obligation):
    channel = next(
        item
        for item in live.harness.observer.enrichment_manifests()
        if isinstance(item, (HookChannelManifest, OtelChannelManifest))
        and item.declaration.id == obligation.detail
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
    effect = await mcp_effect(live, participant)
    # The mock numbers calls within each reply; this turn scripts exactly one.
    call_id = "toolu_mock_0" if live.protocol == "anthropic" else "call_mock_0"

    async with capture_channel_facts(live.client, participant) as page:
        turn_started_at = time.time()
        await expect_runs(live, participant, effect)
        await expect_channel_tool_facts(
            live.client,
            participant,
            channel,
            page,
            call_id,
            turn_started_at=turn_started_at,
            evidence=live.session.evidence_dir / f"{live.label}.channels.json",
        )
