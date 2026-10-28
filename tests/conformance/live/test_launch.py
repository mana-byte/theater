"""Each declared approval launches through the real planner; a launch prompt is its own job."""

from __future__ import annotations

import uuid

import pytest

from scripts.harness_compat.contract import MockReply
from theater.constants.harness import HARNESS_APPROVAL_POLICIES

pytestmark = [pytest.mark.integration, pytest.mark.family("launch")]


async def test_the_stock_cli_launches_and_reaches_a_ready_prompt(live, obligation):
    if obligation.detail == "prompt":
        await _launches_with_an_initial_prompt(live)
        return

    participant = await live.spawn(obligation.detail)

    await live.ready(participant)

    await live.expect_prompt(participant)
    assert (await live.participant(participant)).status != "dead"


async def _launches_with_an_initial_prompt(live) -> None:
    """The launch prompt reaches the model once and finishes the spawn job with its answer."""
    nonce = f"ANSWER-{uuid.uuid4().hex[:8]}"
    prompt = f"Reply with {nonce}"
    live.queue(MockReply(nonce))
    # The least restrictive approval: no gating machinery between launch and the prompt's turn.
    declared = live.harness.launch_parameter_support.approvals
    approval = next(policy for policy in reversed(HARNESS_APPROVAL_POLICIES) if policy in declared)
    participant = await live.spawn(approval, prompt=prompt)
    # A spawn's job handle is its participant id: one task handle, the launch prompt's turn.
    job = await live.job(participant)

    assert job.state == "done", job.error
    assert nonce in str(job.result)
    assert len(live.turns(prompt)) == 1
