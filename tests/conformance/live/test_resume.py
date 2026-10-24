"""A resumed participant carries the old conversation forward and submits each prompt once."""

from __future__ import annotations

import uuid

import pytest

from scripts.harness_compat.contract import MockReply
from tests.support.live_rig import violation

pytestmark = [pytest.mark.integration, pytest.mark.family("resume")]


async def test_a_resumed_participant_continues_its_conversation(live, obligation):
    first = await live.spawn(live.approval)
    await live.ready(first)
    earlier = f"Remember {uuid.uuid4().hex[:8]}"
    live.queue(MockReply("noted"))
    assert (await live.send(first, earlier)).state == "done"
    await live.until(first, lambda p: p.transcript_identity is not None, what="bound a transcript")
    await live.retire(first)
    resumable = await live.until(
        first, lambda p: p.resume_state not in (None, "live"), what="reported a resume verdict"
    )
    if resumable.resume_state != "resumable":
        violation(f"the daemon refused to resume a finished session: {resumable.resume_state}")

    second = await live.spawn(live.approval, resume=first)
    await live.ready(second)
    nonce = f"AFTER-{uuid.uuid4().hex[:8]}"
    prompt = f"Reply with {nonce}"
    live.queue(MockReply(nonce))
    job = await live.send(second, prompt)

    assert job.state == "done", job.error
    assert nonce in str(job.result)
    (resumed,) = [request for request in live.mock.requests if request.last_user == prompt]
    assert earlier in resumed.user_texts, "the resumed conversation lost its history"
    assert len([r for r in live.mock.requests if r.last_user == earlier]) == 1, (
        "the earlier prompt was submitted again"
    )
