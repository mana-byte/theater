"""A resumed participant carries the old conversation forward and submits each prompt once."""

from __future__ import annotations

import uuid

import pytest

from harness_compat.contract import MockReply
from harness_compat.support.live_rig import violation
from harness_compat.support.transcript_tail import transcript_tail

pytestmark = [pytest.mark.integration, pytest.mark.family("resume")]


async def _transcript_evidence(live, participant_id: str) -> str:
    """Where the resumed turn's records sit relative to the launch: CI keeps nothing else."""
    participant = await live.participant(participant_id)
    identity = participant.transcript_identity
    location = identity.location if identity is not None else None
    return f"; transcript: {transcript_tail(location, launched_at=participant.created_at)}"


async def test_a_resumed_participant_continues_its_conversation(live, obligation):
    first = await live.spawn(live.approval)
    await live.ready(first)
    # One scenario per obligation; this history exceeds the seal's former 32-message limit.
    prompts = [f"Remember {uuid.uuid4().hex[:8]}" for _ in range(33)]
    for history_prompt in prompts:
        live.queue(MockReply("noted"))
        history_job = await live.send(first, history_prompt)
        assert history_job.state == "done", history_job.error
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
    answered = nonce in str(job.result)
    seen = "" if answered else await _transcript_evidence(live, second)
    # The error's code, not its prose: a CI report clips this message, and the records must fit.
    code = job.error.code if job.error is not None else None
    assert answered, f"the resumed send answered {job.result!r}; error {code}{seen}"
    (resumed,) = live.turns(prompt)
    assert all(previous in resumed.user_texts for previous in prompts), (
        "the resumed conversation lost its history"
    )
    successor = await live.participant(second)
    assert successor.transcript_identity is not None
    assert successor.transcript_identity.state == "trusted", successor.transcript_identity
    lineage = await live.until(
        first, lambda p: p.resume_state == "owned_by_live", what="been continued by its successor"
    )
    assert lineage.resume_state == "owned_by_live"
    assert all(len(live.turns(previous)) == 1 for previous in prompts), (
        "an earlier prompt was submitted again"
    )
