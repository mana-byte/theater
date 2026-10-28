"""A prompt becomes a done job carrying the scripted answer, delivered to the model once."""

from __future__ import annotations

import uuid

import pytest

from harness_compat.contract import MockReply

pytestmark = [pytest.mark.integration, pytest.mark.family("turn")]


async def test_a_prompt_becomes_a_done_job_with_the_scripted_answer(live, obligation):
    participant = await live.spawn(live.approval)
    await live.ready(participant)
    nonce = f"ANSWER-{uuid.uuid4().hex[:8]}"
    prompt = f"Reply with {nonce}"
    live.queue(MockReply(nonce))

    job = await live.send(participant, prompt)

    assert job.state == "done", job.error
    assert nonce in str(job.result)
    assert len(live.turns(prompt)) == 1
