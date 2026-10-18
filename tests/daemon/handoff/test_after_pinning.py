"""Completion evidence pins Git once, in a thread, before the terminal write."""

from __future__ import annotations

import asyncio
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.handoff_spawn import (
    commit_file,
    defer_spawn,
    init_repository,
    run_git,
    start_source,
)
from tests.support.native_rpc import native_pair
from theater.daemon.handoff import gitfacts
from theater.daemon.observation.reducer import QuietClock
from theater.models import JobState

pytestmark = pytest.mark.usefixtures("available_harness_binaries")


async def test_completion_evidence_is_pinned_before_the_terminal_unit_opens(
    daemon, launches, tmp_path
):
    repo = init_repository(tmp_path)
    start_source(daemon, repo)
    await defer_spawn(daemon, repo)
    head = run_git(repo, "rev-parse", "HEAD")

    pinned = await daemon.jobs.prepare_pins("src")
    with daemon.jobs.stage_hashes("src", ()):
        commit_file(repo, "after-staging.py")  # a later commit must not leak into the pin
        with daemon.store.write_unit() as unit:
            daemon.jobs.finish(
                "src#1", state=JobState.DONE, result="ok", connection=unit.connection
            )
    daemon.jobs.release_pins(pinned)

    evidence = daemon.store.get_job_evidence("src#1")
    assert (evidence.git.commit_sha, evidence.outcome, evidence.answer) == (head, "done", "ok")

    # Without staging the unit may not fork Git: the evidence says so instead of guessing.
    daemon.jobs.create(handle="src#2", caller_id="cli", target_id="src", kind="send", prompt="more")
    await defer_spawn(daemon, repo, after="src#2")
    with daemon.store.write_unit() as unit:
        daemon.jobs.finish("src#2", state=JobState.DONE, result="ok", connection=unit.connection)
    unstaged = daemon.store.get_job_evidence("src#2")
    assert (unstaged.git.pin_state, unstaged.git.reason) == ("unpinned", "not_staged")


def _counting_git(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = gitfacts._git

    def counted(argv, **kwargs):
        calls.append(argv)
        return real(argv, **kwargs)

    monkeypatch.setattr(gitfacts, "_git", counted)
    return calls


def test_one_git_process_pins_commit_branch_and_every_dirty_state(tmp_path, monkeypatch):
    repo = init_repository(tmp_path)
    head = run_git(repo, "rev-parse", "HEAD")
    calls = _counting_git(monkeypatch)

    clean = gitfacts.pin_git_facts(str(repo))
    assert (clean.pin_state, clean.commit_sha, clean.branch, clean.dirty_entries) == (
        "pinned",
        head,
        "main",
        0,
    )
    assert len(calls) == 1

    (repo / "README.md").write_text("changed\n")
    (repo / "new.txt").write_text("untracked")
    dirty = gitfacts.pin_git_facts(str(repo))
    assert (dirty.pin_state, dirty.dirty_entries, dirty.commit_sha) == ("dirty", 2, head)

    run_git(repo, "checkout", "--detach", "-q", head)
    assert gitfacts.pin_git_facts(str(repo)).branch is None

    worktree = tmp_path / "linked"
    run_git(repo, "worktree", "add", "-q", str(worktree), "-b", "feature")
    linked = gitfacts.pin_git_facts(str(worktree))
    assert (linked.branch, linked.commit_sha) == ("feature", head)

    empty = tmp_path / "empty"
    subprocess.run(["git", "init", "-q", str(empty)], check=True)
    assert gitfacts.pin_git_facts(str(empty)).reason == "no_commit"
    assert gitfacts.pin_git_facts(str(tmp_path / "nowhere")).reason == "workspace_missing"


async def test_terminal_pins_run_in_a_thread_before_the_unit_and_once_per_workspace(
    daemon, tmp_path, monkeypatch
):
    repo = init_repository(tmp_path)
    daemon.registry.create_spawned(harness="codex", cwd=str(repo), pid="src", name="Writer")
    for handle in ("src#1", "src#2"):
        daemon.jobs.create(
            handle=handle, caller_id="cli", target_id="src", kind="send", prompt="work"
        )
    threads: list[int] = []
    real = daemon.jobs.evidence.pin

    def pin(cwd):
        threads.append(threading.get_ident())
        return real(cwd)

    monkeypatch.setattr(daemon.jobs.evidence, "pin", pin)
    monkeypatch.setattr(daemon.jobs.evidence, "required", lambda job: True)

    pinned = await daemon.jobs.prepare_pins("src")
    with daemon.jobs.stage_hashes("src", ()), daemon.store.write_unit() as unit:
        for handle in ("src#1", "src#2"):
            daemon.jobs.finish(handle, state=JobState.DONE, result="ok", connection=unit.connection)
    daemon.jobs.release_pins(pinned)

    assert len(threads) == 1 and threads[0] != threading.get_ident()
    head = run_git(repo, "rev-parse", "HEAD")
    assert [daemon.store.get_job_evidence(h).git.commit_sha for h in ("src#1", "src#2")] == [
        head,
        head,
    ]


async def test_pin_reads_store_on_the_loop_and_runs_git_on_a_worker(daemon, tmp_path, monkeypatch):
    from theater.daemon.handoff import evidence as evidence_module

    repo = init_repository(tmp_path)
    daemon.registry.create_spawned(harness="codex", cwd=str(repo), pid="src", name="Writer")
    daemon.jobs.create(handle="src#1", caller_id="cli", target_id="src", kind="send", prompt="w")
    loop_thread = threading.get_ident()
    store_threads: list[int] = []
    git_threads: list[int] = []
    real_get = daemon.store.get_participant
    real_pin = evidence_module.pin_git_facts

    def get_participant(*args, **kwargs):
        store_threads.append(threading.get_ident())
        return real_get(*args, **kwargs)

    def pin_git_facts(cwd):
        git_threads.append(threading.get_ident())
        assert isinstance(cwd, str)
        return real_pin(cwd)

    monkeypatch.setattr(daemon.store, "get_participant", get_participant)
    monkeypatch.setattr(evidence_module, "pin_git_facts", pin_git_facts)
    monkeypatch.setattr(daemon.jobs.evidence, "required", lambda job: True)

    pinned = await daemon.jobs.prepare_pins("src")

    assert store_threads and set(store_threads) == {loop_thread}
    assert git_threads and loop_thread not in git_threads
    assert daemon.jobs._prepinned["src#1"].commit_sha == run_git(repo, "rev-parse", "HEAD")
    daemon.jobs.release_pins(pinned)


async def test_release_during_the_thread_hop_leaves_no_referenced_job_without_facts(
    daemon, tmp_path, monkeypatch
):
    repo = init_repository(tmp_path)
    daemon.registry.create_spawned(harness="codex", cwd=str(repo), pid="src", name="Writer")
    daemon.jobs.create(handle="src#1", caller_id="cli", target_id="src", kind="send", prompt="w")
    monkeypatch.setattr(daemon.jobs.evidence, "required", lambda job: True)
    first = await daemon.jobs.prepare_pins("src")
    daemon.jobs.create(handle="src#2", caller_id="cli", target_id="src", kind="send", prompt="w")
    loop = asyncio.get_running_loop()
    real = daemon.jobs.evidence.pin

    def pin(cwd):
        released = threading.Event()
        loop.call_soon_threadsafe(lambda: (daemon.jobs.release_pins(first), released.set()))
        assert released.wait(5)
        return real(cwd)

    monkeypatch.setattr(daemon.jobs.evidence, "pin", pin)

    second = await daemon.jobs.prepare_pins("src")

    assert set(second) == {"src#1", "src#2"}
    assert all(handle in daemon.jobs._prepinned for handle in second)
    daemon.jobs.release_pins(second)
    assert not daemon.jobs._prepinned and not daemon.jobs._pin_refs


def _git_spy(monkeypatch) -> list[int]:
    """Thread ids of every Git process the pinner starts."""
    threads: list[int] = []
    real = gitfacts._git

    def spy(argv, **kwargs):
        threads.append(threading.get_ident())
        return real(argv, **kwargs)

    monkeypatch.setattr(gitfacts, "_git", spy)
    return threads


async def test_cancelled_and_queued_work_never_invokes_git(
    client, daemon, launches, monkeypatch, absent_presence
):
    """A queued message a gate waits on is cancelled: nothing was delivered, nothing is pinned."""
    parent, child, state = await native_pair(daemon)
    state.native_turn_id = "busy"
    stranger = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    queued = await client.call("send", target=child.id, prompt="hi", caller_id=stranger.id)
    assert queued["delivery"] == "queued"
    repo = Path(daemon.registry.get(child.id).cwd)
    await defer_spawn(daemon, repo, after=queued["handle"], parent_id=parent.id)
    threads = _git_spy(monkeypatch)

    assert daemon.jobs.evidence.required(daemon.store.get_job(queued["handle"])) is False
    await daemon.controls.cancel_job(queued["handle"], requester_id=stranger.id)

    assert threads == [] and daemon.store.get_job_evidence(queued["handle"]) is None


async def test_only_a_job_that_may_have_reached_its_target_can_need_a_pin(
    client, daemon, absent_presence
):
    parent, child, state = await native_pair(daemon)
    stranger = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    delivered = await client.call("send", target=child.id, prompt="go", caller_id=parent.id)
    state.native_turn_id = "busy"
    refused = await client.call("send", target=child.id, prompt="later", caller_id=stranger.id)
    await daemon.controls.cancel_job(refused["handle"], requester_id=stranger.id)
    queued = await client.call("send", target=child.id, prompt="again", caller_id=stranger.id)

    pinner = daemon.jobs.evidence
    assert pinner.delivered(daemon.store.get_job(delivered["handle"])) is True
    assert pinner.delivered(daemon.store.get_job(refused["handle"])) is False
    assert pinner.delivered(daemon.store.get_job(queued["handle"])) is False


async def test_native_terminal_evidence_pins_in_a_thread_before_the_job_is_finished(
    daemon, launches, tmp_path, monkeypatch
):
    repo = init_repository(tmp_path)
    start_source(daemon, repo)
    await defer_spawn(daemon, repo)
    threads = _git_spy(monkeypatch)

    async def sink(pid, *, backend_generation, outcome):
        daemon.jobs.finish("src#1", state=JobState.DONE, result="from evidence")

    registration = SimpleNamespace(evidence_sink=sink, backend_generation=1)
    batch = SimpleNamespace(terminal_evidence=[object()])
    routed = await daemon.observer._route_terminal_evidence("src", None, batch, registration)

    evidence = daemon.store.get_job_evidence("src#1")
    assert routed is True
    assert evidence.git.pin_state == "pinned" and evidence.git.commit_sha
    assert threads and threading.get_ident() not in threads, "Git ran off the event loop"


async def test_a_path_nobody_staged_fails_closed_without_running_git_on_the_loop(
    daemon, launches, tmp_path, monkeypatch
):
    repo = init_repository(tmp_path)
    start_source(daemon, repo)
    await defer_spawn(daemon, repo)
    threads = _git_spy(monkeypatch)

    daemon.jobs.finish("src#1", state=JobState.DONE, result="ok")  # a sync path, on the loop

    evidence = daemon.store.get_job_evidence("src#1")
    assert (evidence.git.pin_state, evidence.git.reason) == ("unpinned", "not_staged")
    assert threads == []


async def _review_source(daemon, tmp_path):
    repo = init_repository(tmp_path)
    start_source(daemon, repo)
    await defer_spawn(
        daemon, repo, task_kind="independent_review", worktree=True, prompt="Review it"
    )
    return repo


def _assert_pinned_off_loop(daemon, threads) -> None:
    git = daemon.store.get_job_evidence("src#1").git
    assert git.pin_state == "pinned" and git.commit_sha
    assert threads and threading.get_ident() not in threads, "Git ran off the event loop"


def _finish_source(daemon):
    return lambda *_a, **_k: daemon.jobs.finish("src#1", state=JobState.DONE, result="ok") or True


async def test_an_attach_time_turn_end_pins_git_off_the_loop(
    daemon, launches, tmp_path, monkeypatch
):
    await _review_source(daemon, tmp_path)
    threads = _git_spy(monkeypatch)
    monkeypatch.setattr(daemon.observer, "_accept_attachment", _finish_source(daemon))
    batch = SimpleNamespace(
        events=[], attached=SimpleNamespace(last_event=SimpleNamespace(turn_end=True))
    )
    context = SimpleNamespace(pid="src", source=None, registration=None)

    assert await daemon.observer._handle_rejected_attachment(context, batch) is False

    _assert_pinned_off_loop(daemon, threads)


async def test_an_attach_without_a_turn_end_costs_no_git(daemon, launches, tmp_path, monkeypatch):
    await _review_source(daemon, tmp_path)
    threads = _git_spy(monkeypatch)
    monkeypatch.setattr(daemon.observer, "_accept_attachment", lambda *_a, **_k: True)
    batch = SimpleNamespace(
        events=[], attached=SimpleNamespace(last_event=SimpleNamespace(turn_end=False))
    )

    await daemon.observer._handle_rejected_attachment(
        SimpleNamespace(pid="src", source=None, registration=None), batch
    )

    assert threads == []


async def test_a_relocated_transcript_turn_end_pins_git_off_the_loop(
    daemon, launches, tmp_path, monkeypatch
):
    await _review_source(daemon, tmp_path)
    threads = _git_spy(monkeypatch)
    reducer = daemon.observer._reducer
    monkeypatch.setattr(type(reducer), "relocate", property(lambda _self: -1.0))
    batch = SimpleNamespace(events=[SimpleNamespace(turn_end=True)], attached=None)

    async def refresh():
        return batch

    async def on_progress(*_a):
        return None

    await reducer.on_quiet(
        "src",
        None,
        SimpleNamespace(refresh=refresh, discard_attachment=lambda: None),
        QuietClock(),
        None,
        validate_batch_fn=lambda *_a: None,
        report_source_error_fn=lambda *_a: None,
        accept_attachment_fn=lambda *_a: True,
        apply_fn=_finish_source(daemon),
        on_progress_fn=on_progress,
        evidence_bound_fn=None,
        confirm_identity_loss_fn=None,
        mark_identity_lost_fn=None,
        reset_identity_loss_fn=None,
        is_untrusted_rotation_fn=lambda *_a: False,
        rescue_jobs_fn=None,
        pinned_fn=daemon.observer._pinned_for,
    )

    _assert_pinned_off_loop(daemon, threads)


async def test_recorded_native_evidence_pins_git_before_finishing_without_an_observer(
    daemon, launches, tmp_path, monkeypatch
):
    """A buffered outcome reaches the sink on reconnect, with no observer pin around it."""
    await _review_source(daemon, tmp_path)
    threads = _git_spy(monkeypatch)
    finish = _finish_source(daemon)
    monkeypatch.setattr(daemon.controls, "_record_terminal_evidence_locked", finish)

    await daemon.controls.record_terminal_evidence("src", backend_generation=1, outcome=None)

    _assert_pinned_off_loop(daemon, threads)


async def test_a_retained_evidence_retry_pins_git_off_the_loop_and_is_not_replayed(
    daemon, launches, tmp_path, monkeypatch
):
    await _review_source(daemon, tmp_path)
    threads = _git_spy(monkeypatch)
    calls: list[str] = []

    async def sink(pid, *, backend_generation, outcome):
        calls.append(outcome)
        daemon.jobs.finish("src#1", state=JobState.DONE, result="ok")

    daemon.observer._pending_evidence["src"] = {(1, "s", "t"): (sink, SimpleNamespace(), "outcome")}

    assert await daemon.observer._flush_pending_evidence("src") is True
    assert await daemon.observer._flush_pending_evidence("src") is True

    _assert_pinned_off_loop(daemon, threads)
    assert calls == ["outcome"] and "src" not in daemon.observer._pending_evidence


async def test_overlapping_pins_share_one_git_read_until_the_last_release(
    daemon, launches, tmp_path, monkeypatch
):
    await _review_source(daemon, tmp_path)
    threads = _git_spy(monkeypatch)

    async with daemon.jobs.pinned("src"):
        async with daemon.jobs.pinned("src"):
            pass
        assert "src#1" in daemon.jobs._prepinned  # the inner release leaves the outer pin held
    assert not daemon.jobs._prepinned and not daemon.jobs._pin_refs
    assert len(threads) == 1 and threading.get_ident() not in threads
