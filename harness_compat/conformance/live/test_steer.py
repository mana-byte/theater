"""A steer reaches the model inside the running turn, or is refused where declared unavailable."""

from __future__ import annotations

import uuid

import pytest

from harness_compat.contract import MockReply
from theater.frontend.client import FrontendResponseError

pytestmark = [pytest.mark.integration, pytest.mark.family("steer")]


async def test_a_steer_follows_what_the_harness_declares(live, obligation):
    participant = await live.spawn(live.approval)
    await live.ready(participant)
    prompt = f"Work on {uuid.uuid4().hex[:8]}"
    held = await live.hold_turn(participant, prompt)
    steer = f"Steer {uuid.uuid4().hex[:8]}"
    final = f"STEERED-{uuid.uuid4().hex[:8]}"

    if obligation.detail == "refused":
        await _refused(live, participant, steer)
    else:
        await live.steer(participant, steer)
    live.queue(MockReply(final))
    held.hold.release()
    job = await live.job(held.handle)

    assert job.state == "done", job.error
    if obligation.detail == "refused":
        assert not live.turns(steer), "a refused steer still reached the model"
        return
    assert final in str(job.result)
    (original,) = live.turns(prompt)
    (steered,) = live.turns(steer)
    assert prompt in steered.user_texts, "the steer arrived without the turn it amended"
    assert steered.messages > original.messages


async def _refused(live, participant: str, steer: str) -> None:
    offered = (await live.controls(participant)).actions.get("steer")
    assert offered is None or not offered.admissible, f"steer is declared unavailable: {offered}"
    with pytest.raises(FrontendResponseError) as raised:
        await live.client.controls.steer(participant, steer, idempotency_key=uuid.uuid4().hex)
    assert "steer" in str(raised.value).lower(), raised.value
