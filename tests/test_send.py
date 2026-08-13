"""Tests for live delivery via send-keys.

Covers: send to an addressable target, human_present rejection, busy
rejection, send to an unaddressable target, and the full send → await →
result loop.

tmux is stubbed: `deliver_text` and `human_present` are monkeypatched.
The job lifecycle is real — the observer's _answer_turn would
finish the send job if it were connected to a real transcript, but here
the tests finish jobs directly via the JobManager.
"""

from __future__ import annotations

import pytest

from theater.daemon.jobs import JobState
from theater.harness import HARNESSES, normalize
from theater.harness.observation import (
    ScreenConfidence,
    ScreenKind,
    ScreenReading,
)
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


async def test_send_allowed_after_job_exceeds_ttl(client, fake_tmux, daemon, monkeypatch):
    """A running send job older than SEND_CLAIM_TTL no longer blocks the pane.

    The prompt may never have reached the agent (a human cleared the
    composer), and the rescue timer cannot fire on an active participant.
    Past the TTL the job loses its reservation — it is not finished, the
    observer may still answer it, but a new send is accepted.
    """
    import theater.daemon.methods as methods_mod
    from theater.daemon.methods import SEND_CLAIM_TTL

    target = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    await client.call("send", target=target["id"], prompt="first")

    # Drive the clock: the send handler computes `stale = now() - SEND_CLAIM_TTL`
    # and rejects jobs whose created_at is newer than that. By advancing `now`
    # past the TTL, the job's real created_at falls on the stale side and the
    # busy gate drops it.
    real_now = methods_mod.now()
    monkeypatch.setattr(
        methods_mod, "now", lambda: real_now + SEND_CLAIM_TTL + 1
    )

    # The stale job no longer blocks; a new send is accepted.
    job2 = await client.call("send", target=target["id"], prompt="second")
    assert job2["state"] == "running"
    assert len(fake_tmux.sent) == 2
    assert fake_tmux.sent[1] == ("%1", "second")


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


async def test_the_job_exists_before_the_prompt_is_typed(
    client, fake_tmux, daemon, monkeypatch
):
    """The reservation is taken first, so a fast reply has something to land on.

    An agent can finish its turn before the send RPC has even returned. With
    the job created after send-keys, the observer would see that turn end with
    no running job and the caller would then await a promise nobody can keep.
    """
    from theater.tmux import client as tmux_client

    target = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    seen: list[list] = []

    async def spy(pane, text):
        seen.append(daemon.store.running_jobs_for_target(target["id"]))

    monkeypatch.setattr(tmux_client, "deliver_text", spy)
    await client.call("send", target=target["id"], prompt="quick one")
    assert len(seen) == 1 and len(seen[0]) == 1
    assert seen[0][0].prompt == "quick one"


async def test_a_send_that_could_not_be_typed_does_not_wedge_the_target(
    client, fake_tmux, daemon, monkeypatch
):
    """send-keys failed, so nothing will answer: the reservation is released."""
    from theater.tmux import client as tmux_client

    target = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")

    async def broken(pane, text):
        raise RuntimeError("pane went away")

    monkeypatch.setattr(tmux_client, "deliver_text", broken)
    with pytest.raises(RemoteError):
        await client.call("send", target=target["id"], prompt="doomed")
    monkeypatch.setattr(tmux_client, "deliver_text", fake_tmux.deliver_text)

    # No leftover reservation, so the next send is accepted rather than busy.
    job = await client.call("send", target=target["id"], prompt="second")
    assert job["state"] == "running"


async def test_a_bare_spawn_leaves_nothing_running(client, fake_tmux, daemon):
    """A spawn with no prompt asked nothing, so its job is already finished.

    Left running it would block every later send as busy, and eat the first
    turn end the human produces.
    """
    child = await client.call(
        "spawn", harness="vibe", prompt="", approval="manual", cwd="/tmp"
    )
    assert daemon.jobs.get(child["handle"]).state == JobState.DONE
    job = await client.call("send", target=child["id"], prompt="now do something")
    assert job["state"] == "running"


async def test_a_spawn_that_asked_for_something_is_still_pending(
    client, fake_tmux, daemon
):
    """The counterpart: a spawn prompt occupies the pane like any other."""
    child = await client.call(
        "spawn", harness="vibe", prompt="do the thing", approval="manual", cwd="/tmp"
    )
    assert daemon.jobs.get(child["handle"]).state == JobState.RUNNING
    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=child["id"], prompt="and another thing")
    assert exc.value.code == "busy"


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


# ---- approval modal gate -------------------------------------------------


def _patch_screen_reading(monkeypatch, reading: ScreenReading) -> None:
    """Replace the vibe observer's `screen_reading` with one that always returns `reading`.

    The gate captures a pane and hands the text to the target's harness
    observer. Tests cannot produce a real approval modal, so the observer is
    replaced with a stub that returns a fixed reading regardless of capture.
    """
    harness = HARNESSES.get(normalize("vibe"))
    assert harness is not None, "vibe must be registered for these tests"
    monkeypatch.setattr(
        harness.observer, "screen_reading", lambda capture: reading
    )


async def test_send_to_a_pane_showing_an_approval_modal_at_high_confidence_is_refused(
    client, fake_tmux, daemon, monkeypatch
):
    """An approval modal at high confidence blocks the send."""
    _patch_screen_reading(
        monkeypatch,
        ScreenReading(kind=ScreenKind.APPROVAL, confidence=ScreenConfidence.HIGH),
    )
    target = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=target["id"], prompt="go ahead")
    assert exc.value.code == "awaiting_decision"
    assert len(fake_tmux.sent) == 0


async def test_send_to_a_pane_showing_an_approval_modal_at_low_confidence_is_allowed(
    client, fake_tmux, daemon, monkeypatch
):
    """Low confidence is the floor: the gate lets it through rather than risk a false refusal."""
    _patch_screen_reading(
        monkeypatch,
        ScreenReading(kind=ScreenKind.APPROVAL, confidence=ScreenConfidence.LOW),
    )
    target = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    job = await client.call("send", target=target["id"], prompt="go ahead")
    assert job["state"] == "running"
    assert len(fake_tmux.sent) == 1


async def test_send_to_a_pane_whose_screen_reads_unknown_is_allowed(
    client, fake_tmux, daemon, monkeypatch
):
    """Unknown is not approval, so the send proceeds."""
    _patch_screen_reading(
        monkeypatch,
        ScreenReading(kind=ScreenKind.UNKNOWN, confidence=ScreenConfidence.LOW),
    )
    target = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    job = await client.call("send", target=target["id"], prompt="go ahead")
    assert job["state"] == "running"
    assert len(fake_tmux.sent) == 1


async def test_send_is_allowed_when_the_capture_raises(
    client, fake_tmux, daemon, monkeypatch
):
    """A tmux error during capture does not turn into an unreachable pane."""
    import theater.daemon.methods as methods_mod

    async def broken_run(*args, check=True):
        raise RuntimeError("tmux exploded")

    monkeypatch.setattr(methods_mod.tmux, "run", broken_run)
    target = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    job = await client.call("send", target=target["id"], prompt="go ahead")
    assert job["state"] == "running"
    assert len(fake_tmux.sent) == 1


async def test_the_approval_modal_refusal_is_counted_by_stats(
    client, fake_tmux, daemon, monkeypatch
):
    """The refusal flows through `_refuse_send` so `theater stats` sees it."""
    _patch_screen_reading(
        monkeypatch,
        ScreenReading(kind=ScreenKind.APPROVAL, confidence=ScreenConfidence.HIGH),
    )
    target = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    with pytest.raises(RemoteError):
        await client.call("send", target=target["id"], prompt="go ahead")
    assert daemon.store.refusal_counts() == {"awaiting_decision": 1}
