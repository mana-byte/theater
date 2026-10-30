"""Available hooks deliver each declared event and correlated tool facts through the stock CLI.

No scenario exists for an available OTel channel; it is explicitly inconclusive.

The event oracle joins the hook bus's admitted native id to its projected trajectory fact.
"""

from __future__ import annotations

import asyncio
import json
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
        await _event_probe(live, participant, channel, event, obligation.detail)
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


async def _event_probe(live, participant, channel, event, detail):
    """Each event has its own oracle; the first plain event also proves FIFO completion."""
    assert detail in live.compat.hook_events, (
        f"the adapter has no stimulus for declared hook {detail}"
    )
    probe = live.compat.hook_events[detail]
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

        async def observed():
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

        if call is None and event == next(
            binding.event
            for binding in channel.bindings
            if f"{channel.declaration.id}/{binding.event}" in live.compat.hook_events
            and live.compat.hook_events[f"{channel.declaration.id}/{binding.event}"] is None
        ):
            job = await _queued_probe(live, participant, prompt, final)
            await observed()
        else:
            if call is None:
                live.queue(MockReply(final))
            handle = await live.submit(participant, prompt)
            try:
                await observed()
            finally:
                if probe is not None and "release" in probe.placeholders:
                    release.touch()
            job = await live.job(handle, guard=check_recipe)
        await check_recipe()
        assert job.state == "done" and final in str(job.result), job.error


async def _queued_probe(live, participant: str, prompt: str, final: str):
    """Prove FIFO strict/plain jobs through actual provider, transcript and terminal awaits."""
    original_prompt = f"Held original {uuid.uuid4().hex}"
    strict_answer = {"status": "done", "summary": uuid.uuid4().hex, "blockers": []}
    strict_prompt = f"Queued strict probe. Reply only {json.dumps(strict_answer)}"
    held = await live.hold_turn(participant, original_prompt)
    first_hold = live.hold()
    try:
        first = (
            await live.client.controls.queue_followup(
                participant,
                strict_prompt,
                idempotency_key=uuid.uuid4().hex,
                response_format={"$theater": "delegate"},
            )
        ).value.job_handle
        second = (
            await live.client.controls.queue_followup(
                participant, prompt, idempotency_key=uuid.uuid4().hex
            )
        ).value.job_handle
        assert first is not None and second is not None and first != second
        assert tuple((await live.controls(participant)).extra["queued"]) == (first, second)
        assert not live.turns(strict_prompt) and not live.turns(prompt)
        live.queue(MockReply(json.dumps(strict_answer), hold=first_hold))
        live.queue(MockReply(final))
        held.hold.release()
        assert await asyncio.to_thread(first_hold.wait_reached, 60), "first followup never ran"
        original = await live.job(held.handle)
        assert original.state == "done" and original.finished_at is not None, original.error
        assert (await live.client.jobs.get(first)).value.state == "running"
        assert tuple((await live.controls(participant)).extra["queued"]) == (second,)
        assert not live.turns(prompt), "second followup overtook the first job"
        first_hold.release()
        strict_job = await live.job(first)
        plain_job = await live.job(second)
        for handle, job in ((first, strict_job), (second, plain_job)):
            assert job.handle == handle and job.target_id == participant
            assert job.state == "done" and job.finished_at is not None, job.error
            assert job.extra["await_reason"] in ("job_terminal", "presence_released")
        assert strict_job.extra["structured_status"] == "parsed"
        assert strict_job.to_wire()["result"] == strict_answer
        assert plain_job.result == final
        assert not (await live.controls(participant)).extra["queued"]
        (root,) = live.turns(original_prompt)
        (strict,) = live.turns(strict_prompt)
        (plain,) = live.turns(prompt)
        assert root.messages < strict.messages < plain.messages
        return plain_job
    finally:
        held.hold.release()
        first_hold.release()
