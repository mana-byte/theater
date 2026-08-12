"""Tests for live delivery via send-keys.

Covers: send to an addressable target, human_present rejection, busy
rejection, send to an unaddressable target, and the full send → await →
result loop.

tmux is stubbed: `send_keys` and `human_present` are monkeypatched.
The job lifecycle is real — the observer's _finish_jobs_for_turn would
finish the send job if it were connected to a real transcript, but here
the tests finish jobs directly via the JobManager.
"""

from __future__ import annotations

import pytest

from theater.daemon.jobs import JobState
from theater.protocol import RemoteError


async def test_send_creates_a_running_job(client, fake_tmux):
    """send to an addressable target creates a job and delivers the prompt."""
    target = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    job = await client.call("send", target=target["id"], prompt="do the thing")
    assert job["state"] == "running"
    assert job["kind"] == "send"
    assert job["prompt"] == "do the thing"
    assert job["target_id"] == target["id"]
    # The prompt was delivered via send-keys to the right pane
    assert len(fake_tmux.sent) == 1
    assert fake_tmux.sent[0] == ("%1", "do the thing")


async def test_send_to_unaddressable_rejected(client, fake_tmux):
    """send to an External (no pane) participant is rejected."""
    ext = await client.call("hello", harness="vibe", cwd="/tmp")  # no pane = external
    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=ext["id"], prompt="hi")
    assert exc.value.code == "not_addressable"


async def test_send_with_human_present_rejected(client, fake_tmux, monkeypatch):
    """send to a pane where a human is present returns human_present."""
    target = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    import theater.daemon.methods as methods_mod

    async def human_here(pane_id):
        return True

    monkeypatch.setattr(methods_mod, "human_present", human_here)
    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=target["id"], prompt="hi")
    assert exc.value.code == "human_present"
    # Nothing was sent
    assert len(fake_tmux.sent) == 0


async def test_send_to_busy_target_rejected(client, fake_tmux, daemon):
    """send to a target that already has a running send job is rejected."""
    target = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    # First send succeeds
    await client.call("send", target=target["id"], prompt="first")
    # Second send to the same target is rejected
    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=target["id"], prompt="second")
    assert exc.value.code == "busy"
    # Only the first prompt was delivered
    assert len(fake_tmux.sent) == 1
    assert fake_tmux.sent[0] == ("%1", "first")


async def test_send_then_await_result(client, fake_tmux, daemon):
    """send → await → result: the full live-delivery loop."""
    target = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    job = await client.call("send", target=target["id"], prompt="what is 2+2")
    handle = job["handle"]

    # The observer detects turn-end and finishes the job.
    daemon.jobs.finish(handle, state=JobState.DONE, result="4")

    jobs = await client.call("jobs.await", handles=[handle], max_wait=2.0)
    assert len(jobs) == 1
    assert jobs[0]["state"] == "done"
    assert jobs[0]["result"] == "4"


async def test_send_after_job_finishes_allows_resend(client, fake_tmux, daemon):
    """After a send job finishes, a new send to the same target works."""
    target = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    job1 = await client.call("send", target=target["id"], prompt="first")
    daemon.jobs.finish(job1["handle"], state=JobState.DONE, result="done")

    # Second send should succeed now
    job2 = await client.call("send", target=target["id"], prompt="second")
    assert job2["state"] == "running"
    assert len(fake_tmux.sent) == 2


async def test_send_bus_event(client, fake_tmux, daemon):
    """send creates an agent.send bus event."""
    target = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    await client.call("send", target=target["id"], prompt="hi there")

    events = await client.call("bus.tail", limit=100)
    kinds = [e["kind"] for e in events]
    assert "agent.send" in kinds
    send_event = next(e for e in events if e["kind"] == "agent.send")
    assert send_event["to_id"] == target["id"]
    assert "hi there" in send_event["payload"]["prompt"]
