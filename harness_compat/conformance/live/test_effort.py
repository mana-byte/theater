"""A reasoning-effort selection changes the effort the CLI asks for, and the same one is stable."""

from __future__ import annotations

import uuid

import pytest

from harness_compat.contract import MockReply
from harness_compat.support.live_rig import inconclusive, violation

pytestmark = [pytest.mark.integration, pytest.mark.family("effort")]


async def _wire_effort(live, selector: str) -> str | None:
    participant = await live.spawn(live.approval, reasoning_effort=selector)
    await live.ready(participant)
    nonce = f"EFFORT-{uuid.uuid4().hex[:8]}"
    prompt = f"Reply with {nonce}"
    live.queue(MockReply(nonce))
    job = await live.send(participant, prompt)
    assert job.state == "done", job.error
    await live.retire(participant)
    (request,) = live.turns(prompt)
    return request.effort


async def test_an_effort_selection_takes_effect_on_the_wire(live, obligation):
    if len(live.provisioned.efforts) < 2:
        inconclusive("compat.provision offers fewer than two efforts to select between")
    first, second = live.provisioned.efforts[:2]

    wire_first = await _wire_effort(live, first)
    wire_second = await _wire_effort(live, second)
    wire_first_again = await _wire_effort(live, first)

    if None in (wire_first, wire_second):
        violation(
            f"the CLI sent no effort for {first!r} or {second!r}: {wire_first}, {wire_second}"
        )
    assert wire_first != wire_second
    assert wire_first == wire_first_again
