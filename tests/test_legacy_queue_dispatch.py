"""Wave 3A correction: legacy queue dispatch through composed control gates.

``ControlService.queue_followup`` creates its job RUNNING before dispatch, so
the composed ``_legacy_busy_check`` must judge busy on the active-job seam
(``store.active_running_jobs_for_target``), not on every running job —
otherwise the queue head busy-refuses against its own fresh prompt job and no
legacy followup ever dispatches. These tests drive the daemon-composed
ControlService gates (``build_control_gates``) end to end on the fake tmux
rig: no hand-built noop gates, and every busy judgement comes from the
daemon's own store, registry, and jobs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from theater.constants.daemon import SEND_CLAIM_TTL_SECONDS, SEND_SUPERSEDED_ERROR_CODE
from theater.daemon.runtime import control_gates
from theater.daemon.schema import control_operations as control_operations_table
from theater.daemon.server import Daemon
from theater.daemon.spawning.models import SpawnRequest
from theater.harness import HARNESSES, Harness
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    DeliveryResult,
)
from theater.harness.observation import TranscriptObserver
from theater.models import Busy, JobState


class _Obs(TranscriptObserver):
    """A transcript-less observer; observation is disabled in these tests."""

    has_transcript = False

    def find_transcript(self, *, cwd, session_id=None, after=None):  # pragma: no cover
        return None

    def session_id(self, transcript):  # pragma: no cover
        return None

    def parse(self, line, index, *, clip_text=True):  # pragma: no cover
        return []

    def is_idle_screen(self, capture):  # pragma: no cover
        return False


class _LegacyHarness(Harness):
    """A harness with no runtime manifest: legacy tmux wiring by construction."""

    name = "legacy-queue"
    binary = "legacy-queue"
    icon = "L"

    def __init__(self) -> None:
        self.observer = _Obs()

    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
        model: str | None = None,
        mcp_servers=(),
    ) -> LaunchPlan:
        return LaunchPlan(argv=[self.binary, "--prompt", prompt])


def _request(**kwargs) -> SpawnRequest:
    kwargs.setdefault("harness", "legacy-queue")
    kwargs.setdefault("prompt", "first turn")
    kwargs.setdefault("cwd", "/tmp")
    kwargs.setdefault("approval", "manual")
    return SpawnRequest(**kwargs)


async def _daemon(fake_tmux) -> Daemon:
    # The fake tmux pre-declares live "vibe" panes; the pane identity check
    # would read those rows instead of ours. These tests track their own panes.
    fake_tmux.visible_panes.clear()
    harness = _LegacyHarness()
    d = Daemon(harnesses={})
    # ``Daemon.__init__`` re-installs the shipped harness registry, so the
    # test harness registers itself after construction (``clean_registry``
    # restores the shipped set when the test ends).
    HARNESSES[harness.name] = harness
    await d.start()
    return d


async def _await_scheduled(d: Daemon, pid: str) -> None:
    """Await the dispatch pass ``queue_followup`` scheduled, if one exists.

    The pass is a real task on the loop; awaiting it (instead of polling)
    makes delivery deterministic with no sleeps.
    """
    task = d.controls._dispatch_tasks.get(pid)
    if task is not None:
        await task


def _queue_operation_rows(d: Daemon, pid: str) -> list[dict]:
    return [
        dict(row._mapping)
        for row in d.store.conn.execute(
            select(control_operations_table)
            .where(control_operations_table.c.participant_id == pid)
            .where(control_operations_table.c.kind == "queue_followup")
        ).fetchall()
    ]


def _claim(d: Daemon, pid: str, handle: str) -> None:
    """An ordinary send claim: a running prompt job with no control operation."""
    d.jobs.create(
        handle=handle,
        caller_id="cli",
        target_id=pid,
        kind="send",
        prompt="unanswered claim",
        cwd="/tmp",
    )


# ---- the regression: an idle legacy participant's followup dispatches ------


async def test_idle_legacy_followup_dispatches_through_composed_gates(theater_home, fake_tmux):
    d = await _daemon(fake_tmux)
    try:
        p = await d.spawner.spawn(_request())

        job = await d.controls.queue_followup(p.id, caller_id="cli", prompt="followup one")
        await _await_scheduled(d, p.id)

        # The queue head's own fresh prompt job never counted busy: the exact
        # prompt was delivered, exactly once, to the participant's pane.
        assert fake_tmux.sent == [(p.tmux_pane, "followup one")]

        rows = _queue_operation_rows(d, p.id)
        assert len(rows) == 1
        assert rows[0]["job_handle"] == job.handle
        assert rows[0]["delivery_phase"] == str(ControlDeliveryPhase.SETTLED)
        assert rows[0]["delivery_result"] == str(DeliveryResult.ACCEPTED)

        # The job stays running, awaiting observation of the delivered turn.
        assert d.store.get_job(job.handle).state == JobState.RUNNING
    finally:
        await d.aclose()


async def test_second_fifo_item_waits_for_the_first_completion(theater_home, fake_tmux):
    d = await _daemon(fake_tmux)
    try:
        p = await d.spawner.spawn(_request())

        first = await d.controls.queue_followup(p.id, caller_id="cli", prompt="first followup")
        await _await_scheduled(d, p.id)
        assert fake_tmux.sent == [(p.tmux_pane, "first followup")]

        second = await d.controls.queue_followup(p.id, caller_id="cli", prompt="second followup")
        await _await_scheduled(d, p.id)
        # The first followup was delivered and its job is still running —
        # an active job — so the second item did not deliver.
        assert [text for _, text in fake_tmux.sent] == ["first followup"]
        assert d.store.get_job(second.handle).state == JobState.RUNNING
        assert [operation.job_handle for operation in d.store.queued_control_operations(p.id)] == [
            second.handle
        ]

        # A pass while the first turn is still unobserved refuses busy.
        with pytest.raises(Busy):
            await d.controls.dispatch_queue(p.id)
        assert [text for _, text in fake_tmux.sent] == ["first followup"]

        # Observation completes the first followup's turn, and only then does
        # the next pass dispatch the second FIFO item.
        d.jobs.finish(first.handle, state=JobState.DONE, result="")
        outcome = await d.controls.dispatch_queue(p.id)
        assert outcome.dispatched == (second.handle,)
        assert [text for _, text in fake_tmux.sent] == [
            "first followup",
            "second followup",
        ]
    finally:
        await d.aclose()


# ---- the seam is precise, not a blanket ignore ------------------------------


async def test_active_job_seam_excludes_queued_followups_but_keeps_claims(theater_home, fake_tmux):
    d = await _daemon(fake_tmux)
    try:
        p = await d.spawner.spawn(_request())

        queued = await d.controls.queue_followup(p.id, caller_id="cli", prompt="queued followup")
        # The claim is created before any await, so the queued item cannot
        # dispatch past this point and its QUEUED phase is what is asserted.
        _claim(d, p.id, "claim")

        # The queued followup's job is running but never active...
        assert [job.handle for job in d.store.active_running_jobs_for_target(p.id)] == ["claim"]
        assert sorted(job.handle for job in d.store.running_jobs_for_target(p.id)) == sorted(
            [queued.handle, "claim"]
        )
    finally:
        await d.aclose()


async def test_fresh_active_legacy_claim_blocks_queue_dispatch(theater_home, fake_tmux):
    d = await _daemon(fake_tmux)
    try:
        p = await d.spawner.spawn(_request())
        _claim(d, p.id, "claim")

        job = await d.controls.queue_followup(p.id, caller_id="cli", prompt="queued followup")
        await _await_scheduled(d, p.id)

        # The fresh claim still busy-refuses the dispatch...
        assert fake_tmux.sent == []
        assert d.store.get_job("claim").state == JobState.RUNNING
        with pytest.raises(Busy):
            await d.controls.dispatch_queue(p.id)
        assert [operation.job_handle for operation in d.store.queued_control_operations(p.id)] == [
            job.handle
        ]
        # ...and the queued item itself is untouched, never failed.
        assert d.store.get_job(job.handle).state == JobState.RUNNING
    finally:
        await d.aclose()


async def test_stale_active_legacy_claim_is_superseded_and_dispatch_proceeds(
    theater_home, fake_tmux, monkeypatch
):
    d = await _daemon(fake_tmux)
    try:
        p = await d.spawner.spawn(_request())
        _claim(d, p.id, "claim")

        # Drive the clock past the send-claim TTL, exactly like the send RPC's
        # TTL tests: the claim's real created_at falls on the stale side and
        # the busy gate drops it.
        real_now = control_gates.now()
        monkeypatch.setattr(control_gates, "now", lambda: real_now + SEND_CLAIM_TTL_SECONDS + 1)

        job = await d.controls.queue_followup(p.id, caller_id="cli", prompt="queued followup")
        await _await_scheduled(d, p.id)

        # The stale claim is closed as superseded, and the followup delivered.
        claim = d.store.get_job("claim")
        assert claim.state == JobState.CRASHED
        assert claim.error_code == SEND_SUPERSEDED_ERROR_CODE
        assert fake_tmux.sent == [(p.tmux_pane, "queued followup")]
        assert d.store.get_job(job.handle).state == JobState.RUNNING
    finally:
        await d.aclose()
