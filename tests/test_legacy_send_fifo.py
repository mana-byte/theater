"""FIFO regression: an ordinary legacy send cannot overtake queued followups.

``_legacy_busy_check`` deliberately counts only ACTIVE (already-delivered)
jobs, so a QUEUED-but-undelivered followup is invisible to it. Without a
queue guard in ``_send_legacy``, an ordinary send was accepted while a
followup sat queued — typed into the pane AHEAD of it — and the reply then
matched nothing, leaving both jobs running and the queue stalled. The guard
mirrors the native ``_reject_busy`` refusal: a queued followup is the
actionable reason an ordinary send cannot proceed, and the refusal happens
before any job row, control operation, or pane typing.
"""

from __future__ import annotations

import pytest

from tests.test_legacy_queue_dispatch import _await_scheduled, _daemon, _request
from theater.models import Busy, JobState, Status


async def _queued_followup_behind_working_status(d, pid: str):
    """Hold one followup QUEUED behind a WORKING status, then clear it.

    Returns the queued job's handle. The WORKING status is what observation
    of a live turn would set; clearing it to IDLE with no delivered job is
    the exact window where an ordinary send could previously jump the queue.
    """
    d.store.set_status(pid, Status.WORKING)
    queued = await d.controls.queue_followup(pid, caller_id="cli", prompt="queued followup")
    await _await_scheduled(d, pid)
    # The scheduled pass deferred on the WORKING status: the followup is
    # queued, undelivered, and its job is already running (the queue seam).
    assert d.store.queued_control_operation_count(pid) == 1
    assert [job.handle for job in d.store.active_running_jobs_for_target(pid)] == []
    d.store.set_status(pid, Status.IDLE)
    return queued.handle


async def test_ordinary_send_refuses_behind_a_queued_followup(theater_home, fake_tmux):
    d = await _daemon(fake_tmux)
    try:
        p = await d.spawner.spawn(_request())
        handle = await _queued_followup_behind_working_status(d, p.id)

        with pytest.raises(Busy) as excinfo:
            await d.controls.send(p.id, caller_id="cli", prompt="direct send")

        # The refusal is the queue's, not the WORKING status's: the message
        # names the queued followup as the actionable reason.
        assert "has 1 queued followup(s)" in str(excinfo.value)

        # Nothing was delivered, and no new job row or send operation exists:
        # the only running job is still the queued followup's own.
        assert fake_tmux.sent == []
        assert [job.handle for job in d.store.running_jobs_for_target(p.id)] == [handle]
        assert [operation.job_handle for operation in d.store.queued_control_operations(p.id)] == [
            handle
        ]
    finally:
        await d.aclose()


async def test_ordinary_send_succeeds_once_the_queue_has_drained(theater_home, fake_tmux):
    d = await _daemon(fake_tmux)
    try:
        p = await d.spawner.spawn(_request())
        handle = await _queued_followup_behind_working_status(d, p.id)

        # Queue dispatch itself still delivers: the guard lives only in
        # _send_legacy, so the head dispatches exactly once.
        outcome = await d.controls.dispatch_queue(p.id)
        assert outcome.deferred is False
        assert outcome.dispatched == (handle,)
        assert outcome.failed == ()
        assert fake_tmux.sent == [(p.tmux_pane, "queued followup")]
        assert d.store.queued_control_operation_count(p.id) == 0

        # The delivered followup's job finishes; the queue is empty.
        d.jobs.finish(handle, state=JobState.DONE, result="")

        # An ordinary send is accepted again, behind the queue it respected.
        direct = await d.controls.send(p.id, caller_id="cli", prompt="direct send")
        assert [text for _, text in fake_tmux.sent] == ["queued followup", "direct send"]
        assert d.store.get_job(direct.handle).state == JobState.RUNNING
    finally:
        await d.aclose()
