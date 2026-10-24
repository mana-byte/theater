"""A model selection changes the model the CLI asks for, and the same selection is stable."""

from __future__ import annotations

import uuid

import pytest

from scripts.harness_compat.contract import MockReply

pytestmark = [pytest.mark.integration, pytest.mark.family("model")]


async def _wire_model(live, selector: str) -> str | None:
    participant = await live.spawn(live.approval, model=selector)
    await live.ready(participant)
    nonce = f"MODEL-{uuid.uuid4().hex[:8]}"
    prompt = f"Reply with {nonce}"
    live.queue(MockReply(nonce))
    job = await live.send(participant, prompt)
    assert job.state == "done", job.error
    await live.retire(participant)
    (request,) = [request for request in live.mock.requests if request.last_user == prompt]
    return request.model


async def test_a_model_selection_takes_effect_on_the_wire(live, obligation):
    first, second = live.provisioned.models[:2]

    wire_first = await _wire_model(live, first)
    wire_second = await _wire_model(live, second)
    wire_first_again = await _wire_model(live, first)

    assert None not in (wire_first, wire_second)
    assert wire_first != wire_second
    assert wire_first == wire_first_again
