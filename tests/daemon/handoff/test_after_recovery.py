"""Handoff gates across restarts, provider outages and failed terminal writes."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import update

from tests.support.handoff_spawn import (
    defer_spawn,
    end_source,
    init_repository,
    launch_all,
    launch_row_count,
    run_git,
    start_source,
)
from tests.support.waiting import wait_until
from theater.daemon.gc import _sweep_stale_running
from theater.daemon.rpc.jobs import _jobs_await
from theater.daemon.schema import jobs as jobs_table
from theater.models import JobState, now

pytestmark = pytest.mark.usefixtures("available_harness_binaries")


async def test_gate_survives_a_restart_and_never_replays_a_launch(daemon, launches, tmp_path):
    repo = init_repository(tmp_path)
    start_source(daemon, repo)
    reply = await defer_spawn(daemon, repo)
    handle = reply["handle"]
    # A crash mid-open: gate says opening, nothing was launched, in-memory wakeups are gone.
    daemon.store.move_handoff_gate(handle, expected=("waiting",), state="opening", updated_at=1.0)
    daemon.jobs._events.clear()

    await daemon.handoff.recover()
    assert daemon.store.get_handoff_gate(handle).state == "waiting"
    assert handle in daemon.jobs._events and launches == []

    daemon.jobs.finish("src#1", state=JobState.DONE, result="ok")
    await launch_all(daemon)
    assert len(launches) == 1

    # A restart after the launch was reserved must not launch again.
    daemon.store.move_handoff_gate(handle, expected=("opened",), state="opening", updated_at=2.0)
    await daemon.handoff.recover()
    await launch_all(daemon)
    assert daemon.store.get_handoff_gate(handle).state == "opened" and len(launches) == 1


async def test_provider_outage_keeps_the_gate_waiting_and_retries_without_replay(
    daemon, launches, tmp_path, monkeypatch
):
    monkeypatch.setattr("theater.daemon.handoff.service.RETRY_MIN_SECONDS", 0.01)
    monkeypatch.setattr("theater.daemon.handoff.service.RETRY_MAX_SECONDS", 0.02)
    online = {"now": False}
    monkeypatch.setattr(
        daemon.terminal_service.connections,
        "health",
        lambda *_: "online" if online["now"] else "offline",
    )
    repo = init_repository(tmp_path)
    start_source(daemon, repo)
    reply = await defer_spawn(daemon, repo)
    handle = reply["handle"]

    daemon.jobs.finish("src#1", state=JobState.DONE, result="ok")
    await wait_until(
        lambda: daemon.store.get_handoff_gate(handle).error_code == "provider_unavailable"
    )

    gate = daemon.store.get_handoff_gate(handle)
    assert gate.state == "waiting" and daemon.store.get_job(handle).state == JobState.RUNNING
    assert launches == [] and launch_row_count(daemon) == 0

    online["now"] = True
    await wait_until(lambda: len(launches) == 1)
    await launch_all(daemon)
    assert daemon.store.get_handoff_gate(handle).state == "opened"
    await asyncio.sleep(0.1)
    assert len(launches) == 1, "a retry never launches twice"


async def test_a_restart_while_the_provider_is_still_away_keeps_waiting_then_launches(
    daemon, launches, tmp_path, monkeypatch
):
    monkeypatch.setattr("theater.daemon.handoff.service.RETRY_MIN_SECONDS", 0.01)
    online = {"now": False}
    monkeypatch.setattr(
        daemon.terminal_service.connections,
        "health",
        lambda *_: "online" if online["now"] else "offline",
    )
    repo = init_repository(tmp_path)
    start_source(daemon, repo)
    handle = (await defer_spawn(daemon, repo))["handle"]
    # The source ended while the daemon was down; the bridge has not reconnected yet.
    with daemon.store.write_unit() as unit:
        unit.connection.execute(
            update(jobs_table).where(jobs_table.c.handle == "src#1").values(state="done")
        )
    daemon.jobs._events.clear()

    await daemon.handoff.recover()
    await wait_until(
        lambda: daemon.store.get_handoff_gate(handle).error_code == "provider_unavailable"
    )
    assert daemon.store.get_handoff_gate(handle).state == "waiting" and launches == []

    online["now"] = True
    await wait_until(lambda: len(launches) == 1)


async def test_a_gate_waiting_on_a_source_the_gc_abandoned_is_woken(daemon, launches, tmp_path):
    repo = init_repository(tmp_path)
    start_source(daemon, repo)
    handle = (await defer_spawn(daemon, repo))["handle"]
    with daemon.store.write_unit() as unit:
        unit.connection.execute(
            update(jobs_table).where(jobs_table.c.handle == "src#1").values(created_at=1.0)
        )

    assert _sweep_stale_running(daemon.store, 100.0, 100, frozenset()) == 1
    assert daemon.store.get_handoff_gate(handle).state == "waiting", "no listener ran"
    daemon.handoff.wake()
    await launch_all(daemon)

    assert daemon.store.get_handoff_gate(handle).state == "opened"
    assert "FAILED source" in launches[0].prompt and "abandoned" in launches[0].prompt


async def test_a_provider_that_does_not_exist_is_refused_where_it_is_asked_for(
    daemon, launches, tmp_path
):
    from theater.daemon.terminals import ProviderUnavailable

    repo = init_repository(tmp_path)
    start_source(daemon, repo)

    with pytest.raises(ProviderUnavailable, match="not_registered"):
        await defer_spawn(daemon, repo, provider="no-such-provider")
    assert len(daemon.registry.list()) == 1 and daemon.store.handoff_gates(("waiting",)) == []


@pytest.mark.parametrize("reason", ["not_registered", "missing_terminal_create_capability"])
async def test_a_permanent_provider_refusal_ends_the_gate_instead_of_retrying(
    daemon, launches, tmp_path, monkeypatch, reason
):
    from theater.daemon.spawning.provider_launch import ParticipantLaunchService
    from theater.daemon.terminals import ProviderUnavailable

    repo = init_repository(tmp_path)
    start_source(daemon, repo)
    handle = (await defer_spawn(daemon, repo))["handle"]

    async def refuse(self, **_):
        raise ProviderUnavailable("provider-tmux", reason)

    monkeypatch.setattr(ParticipantLaunchService, "spawn", refuse)
    await end_source(daemon, state=JobState.DONE, result="ok")
    await daemon.handoff.settle()

    gate = daemon.store.get_handoff_gate(handle)
    job = daemon.store.get_job(handle)
    assert (gate.state, job.state, job.error_code) == (
        "failed",
        JobState.CRASHED,
        "handoff_launch_refused",
    )
    assert reason in gate.error and not daemon.handoff._retrying


async def test_failed_terminal_write_rolls_back_evidence_so_retry_records_its_own_facts(
    daemon, launches, tmp_path
):
    repo = init_repository(tmp_path)
    start_source(daemon, repo)
    await defer_spawn(daemon, repo)
    head = run_git(repo, "rev-parse", "HEAD")
    real = daemon.store.journal.append_group
    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("injected after evidence write")

    daemon.store.journal.append_group = boom
    with pytest.raises(RuntimeError, match="injected"):
        await end_source(daemon, state=JobState.DONE, result="first")
    daemon.store.journal.append_group = real

    assert calls and daemon.store.get_job("src#1").state == JobState.RUNNING
    assert daemon.store.get_job_evidence("src#1") is None

    await end_source(daemon, state=JobState.CRASHED, result="second", error_code="boom")

    job = daemon.store.get_job("src#1")
    evidence = daemon.store.get_job_evidence("src#1")
    assert (job.state, job.result) == (JobState.CRASHED, "second")
    assert (evidence.outcome, evidence.answer, evidence.git.commit_sha) == (
        "crashed",
        "second",
        head,
    )


def _failing_finish(daemon, handle: str):
    """Make the first finish of ``handle`` raise after the gate move, like a crash mid-close."""
    real = daemon.jobs.finish
    calls: list[int] = []

    def finish(h, **kwargs):
        if h == handle and not calls:
            calls.append(1)
            raise RuntimeError("injected after gate move")
        return real(h, **kwargs)

    daemon.jobs.finish = finish
    return calls


async def _assert_awaitable(daemon, handle: str, gate_state: str) -> None:
    entries = await asyncio.wait_for(
        _jobs_await(daemon, {"handles": [handle], "max_wait": 5, "caller_id": "cli"}), 3
    )
    assert entries[0]["await_reason"] == "job_terminal"
    assert entries[0]["handoff"]["state"] == gate_state


async def test_a_failed_job_finish_rolls_the_cancelled_gate_back_and_cancel_retries(
    daemon, launches, tmp_path
):
    repo = init_repository(tmp_path)
    start_source(daemon, repo)
    handle = (await defer_spawn(daemon, repo))["handle"]
    calls = _failing_finish(daemon, handle)

    with pytest.raises(RuntimeError, match="injected"):
        daemon.handoff.cancel_unlaunched(handle)
    assert calls and daemon.store.get_handoff_gate(handle).state == "waiting"
    assert daemon.store.get_job(handle).state == JobState.RUNNING

    assert daemon.handoff.cancel_unlaunched(handle) is True
    assert daemon.store.get_job(handle).state == JobState.KILLED
    await _assert_awaitable(daemon, handle, "cancelled")
    assert launches == []


async def test_a_failed_job_finish_rolls_the_failed_gate_back_and_recovery_ends_it(
    daemon, launches, tmp_path, monkeypatch
):
    from theater.daemon.spawning.provider_launch import ParticipantLaunchService
    from theater.daemon.terminals import ProviderUnavailable

    repo = init_repository(tmp_path)
    start_source(daemon, repo)
    handle = (await defer_spawn(daemon, repo))["handle"]

    async def refuse(self, **_):
        raise ProviderUnavailable("provider-tmux", "not_registered")

    monkeypatch.setattr(ParticipantLaunchService, "spawn", refuse)
    calls = _failing_finish(daemon, handle)
    await end_source(daemon, state=JobState.DONE, result="ok")
    await daemon.handoff.settle()

    assert calls and daemon.store.get_handoff_gate(handle).state == "opening"
    assert daemon.store.get_job(handle).state == JobState.RUNNING

    await daemon.handoff.recover()
    await daemon.handoff.settle()

    assert daemon.store.get_handoff_gate(handle).state == "failed"
    assert daemon.store.get_job(handle).state == JobState.CRASHED
    await _assert_awaitable(daemon, handle, "failed")


async def test_recovery_ends_a_legacy_terminal_gate_that_left_its_job_running(
    daemon, launches, tmp_path
):
    repo = init_repository(tmp_path)
    start_source(daemon, repo)
    handle = (await defer_spawn(daemon, repo))["handle"]
    daemon.store.move_handoff_gate(
        handle, expected=("waiting",), state="cancelled", updated_at=now(), error_code="cancelled"
    )
    assert daemon.store.get_job(handle).state == JobState.RUNNING

    await daemon.handoff.recover()

    assert daemon.store.get_job(handle).state == JobState.KILLED
    await _assert_awaitable(daemon, handle, "cancelled")
    assert launches == []
