"""Durable ``after`` handoffs: deferred spawn, completion evidence, review isolation, awaits."""

from __future__ import annotations

import asyncio
import dataclasses
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update

from tests._presence_doubles import AbsentPresence
from tests.test_control_rpc import _native_pair
from theater.constants.daemon import CONTROL_QUEUE_PEER_MAX_PENDING
from theater.daemon.gc import _sweep_stale_running
from theater.daemon.handoff import gitfacts
from theater.daemon.observation.reducer import QuietClock
from theater.daemon.operations import OperationOutcome
from theater.daemon.rails import BudgetExceeded, CycleDetected
from theater.daemon.rpc.jobs import _jobs_await
from theater.daemon.rpc.spawning import _spawn
from theater.daemon.schema import jobs as jobs_table
from theater.daemon.schema import launch_reservations
from theater.daemon.spawning.models import Reservation
from theater.harness.base import LaunchPlan
from theater.models import BadRequest, Busy, JobState, ProviderRecord, now

pytestmark = pytest.mark.usefixtures("available_harness_binaries")


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return done.stdout.strip()


def _repository(path: Path) -> Path:
    root = path / "repository"
    root.mkdir()
    for args in (
        ("init", "-b", "main"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "T"),
    ):
        _git(root, *args)
    (root / "README.md").write_text("one\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "one")
    return root


async def _end(daemon, handle: str = "src#1", *, target: str = "src", **finish) -> None:
    """Finish a source the way the observer does: pin Git off the loop, then publish."""
    pinned = await daemon.jobs.prepare_pins(target)
    try:
        daemon.jobs.finish(handle, **finish)
    finally:
        daemon.jobs.release_pins(pinned)


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def launches(daemon, monkeypatch) -> list:
    """Fake provider: record each launch request instead of creating a terminal."""
    timestamp = now()
    record = ProviderRecord(
        provider_id="provider-tmux",
        selector="tmux",
        kind="fixture",
        credential_verifier="a" * 64,
        configuration_version=1,
        capabilities=("terminal-provider.v1",),
        limits={},
        generation=1,
        last_report_revision=1,
        created_at=timestamp,
        updated_at=timestamp,
    )
    with daemon.store.write_unit() as unit:
        daemon.store.providers.register(record, connection=unit.connection)
    monkeypatch.setattr(daemon.terminal_service.connections, "is_current", lambda *_: True)
    monkeypatch.setattr(daemon.terminal_service.connections, "health", lambda *_: "online")
    requests: list = []

    async def prepare(req, participant, *, child_cwd, provider, workspace_usage_id, **_):
        requests.append(req)
        return Reservation(
            participant=participant,
            plan=LaunchPlan(argv=["/bin/sh", "--fixture"]),
            child_cwd=child_cwd,
            session="",
            name="fixture",
            req=req,
            provider=provider,
            workspace_usage_id=workspace_usage_id,
        )

    async def dispatch(provider_id, generation, _method, params):
        pid = str(params["participant_id"])
        return OperationOutcome.succeeded(
            phase="provider_acknowledged",
            result={
                "operation_id": params["operation_id"],
                "provider_generation": generation,
                "outcome": "accepted",
                "terminal": {
                    "provider_id": provider_id,
                    "provider_generation": generation,
                    "terminal_id": f"terminal-{pid}",
                    "terminal_incarnation": "incarnation-1",
                    "occupant": {"occupant_id": pid, "harness": "codex"},
                    "process": {"pid": 1234, "started_at": 10.0, "executable": "/bin/agent"},
                },
            },
        )

    monkeypatch.setattr(daemon.spawner, "prepare_provider_launch", prepare)
    monkeypatch.setattr(daemon.terminal_service, "dispatch_operation", dispatch)
    return requests


def _source(daemon, repo: Path, *, handle: str = "src#1") -> None:
    daemon.registry.create_spawned(harness="codex", cwd=str(repo), pid="src", name="Writer")
    daemon.jobs.create(
        handle=handle, caller_id="cli", target_id="src", kind="send", prompt="build the parser"
    )


async def _defer(daemon, repo: Path, **extra) -> dict:
    params = {
        "harness": "codex",
        "approval": "yolo",
        "cwd": str(repo),
        "prompt": "Take it from here",
        "after": "Writer",
        **extra,
    }
    return await _spawn(daemon, params)


async def _launch_all(daemon) -> None:
    await daemon.handoff.settle()
    await asyncio.sleep(0)
    tasks = daemon.operation_service.owned_tasks
    if tasks:
        await asyncio.gather(*tasks)


def _launch_rows(daemon) -> int:
    return daemon.store.conn.execute(select(func.count()).select_from(launch_reservations)).scalar()


async def test_work_task_waits_then_launches_with_a_labelled_card(
    daemon, launches, tmp_path, monkeypatch
):
    repo = _repository(tmp_path)
    _source(daemon, repo)
    emitted: list = []
    import theater.daemon.handoff.service as service_module

    real_emit = service_module.timing.emit
    service_module.timing.emit = lambda spec, ms, **fields: (
        emitted.append(fields) or real_emit(spec, ms, **fields)
    )

    reply = await _defer(daemon, repo)
    handle = reply["handle"]

    assert reply["handoff"] | {"waited_s": 0} == {
        "state": "waiting",
        "task_kind": "work",
        "source": "src#1",
        "source_name": "Writer",
        "waited_s": 0,
    }
    assert launches == [] and _launch_rows(daemon) == 0
    assert daemon.store.get_job(handle).state == JobState.RUNNING

    head = _git(repo, "rev-parse", "HEAD")
    await _end(daemon, state=JobState.DONE, result="PLAN: do X")
    evidence = daemon.store.get_job_evidence("src#1")
    assert (evidence.git.pin_state, evidence.git.commit_sha) == ("pinned", head)

    await _launch_all(daemon)

    (request,) = launches
    assert request.prompt.startswith("Take it from here")
    assert "PLAN: do X" in request.prompt and head in request.prompt
    assert "UNVERIFIED" in request.prompt and "Writer" in request.prompt
    assert daemon.store.get_handoff_gate(handle).state == "opened"
    assert daemon.store.get_job(handle).handle == handle  # the one task handle never changes
    assert emitted[-1]["result"] == "success" and emitted[-1]["task_kind"] == "work"


async def test_independent_review_gets_facts_not_conclusions_and_the_pinned_commit(
    daemon, launches, tmp_path
):
    repo = _repository(tmp_path)
    _source(daemon, repo)
    pinned = _commit(repo, "feature.py")
    reply = await _defer(
        daemon, repo, task_kind="independent_review", worktree=True, prompt="Review it"
    )
    await _end(daemon, state=JobState.DONE, result="SECRET-CONCLUSION: ship it")
    _commit(repo, "later.py")  # HEAD moves on; the review must still use the pinned commit

    await _launch_all(daemon)

    (request,) = launches
    assert "SECRET-CONCLUSION" not in request.prompt and pinned in request.prompt
    workspace = daemon.store.workspaces.get(daemon.registry.get(reply["handle"]).workspace_id)
    assert workspace.resolved_base_commit == pinned != _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("kind", ["work", "independent_review"])
async def test_dirty_source_is_labelled_for_work_and_rejected_for_review(
    daemon, launches, tmp_path, kind
):
    repo = _repository(tmp_path)
    _source(daemon, repo)
    (repo / "scratch.txt").write_text("uncommitted")
    reply = await _defer(daemon, repo, task_kind=kind, worktree=kind == "independent_review")
    await _end(daemon, state=JobState.DONE, result="done")
    await _launch_all(daemon)

    gate = daemon.store.get_handoff_gate(reply["handle"])
    if kind == "work":
        assert "DIRTY" in launches[0].prompt and gate.state == "opened"
    else:
        job = daemon.store.get_job(reply["handle"])
        assert (launches, gate.state, job.state) == ([], "failed", JobState.CRASHED)
        assert job.error_code == "source_not_reviewable" and _launch_rows(daemon) == 0
        assert not (repo.parent / ".theater").exists()


async def test_failed_source_hands_off_a_failure_card(daemon, launches, tmp_path):
    repo = _repository(tmp_path)
    _source(daemon, repo)
    await _defer(daemon, repo)
    daemon.jobs.finish("src#1", state=JobState.CRASHED, result="partial", error_code="crashed")
    await _launch_all(daemon)

    assert "FAILED source" in launches[0].prompt and "crashed" in launches[0].prompt
    assert "Partial output of the failed source" in launches[0].prompt


async def test_gate_survives_a_restart_and_never_replays_a_launch(daemon, launches, tmp_path):
    repo = _repository(tmp_path)
    _source(daemon, repo)
    reply = await _defer(daemon, repo)
    handle = reply["handle"]
    # A crash mid-open: gate says opening, nothing was launched, in-memory wakeups are gone.
    daemon.store.move_handoff_gate(handle, expected=("waiting",), state="opening", updated_at=1.0)
    daemon.jobs._events.clear()

    await daemon.handoff.recover()
    assert daemon.store.get_handoff_gate(handle).state == "waiting"
    assert handle in daemon.jobs._events and launches == []

    daemon.jobs.finish("src#1", state=JobState.DONE, result="ok")
    await _launch_all(daemon)
    assert len(launches) == 1

    # A restart after the launch was reserved must not launch again.
    daemon.store.move_handoff_gate(handle, expected=("opened",), state="opening", updated_at=2.0)
    await daemon.handoff.recover()
    await _launch_all(daemon)
    assert daemon.store.get_handoff_gate(handle).state == "opened" and len(launches) == 1


async def test_a_cancelled_task_releases_its_await_without_a_presence_departure(
    daemon, launches, tmp_path
):
    repo = _repository(tmp_path)
    _source(daemon, repo)
    reply = await _defer(daemon, repo)
    handle = reply["handle"]

    daemon.jobs.finish(handle, state=JobState.KILLED, error_code="killed")
    assert daemon.store.get_handoff_gate(handle).state == "cancelled"

    entries = await asyncio.wait_for(
        _jobs_await(daemon, {"handles": [handle], "max_wait": 5, "caller_id": "cli"}), 3
    )
    assert entries[0]["await_reason"] == "job_terminal" and entries[0]["handoff"]["state"] == (
        "cancelled"
    )
    assert launches == []


async def test_awaiting_a_task_gated_on_the_callers_own_job_is_a_cycle_but_after_it_is_not(
    daemon, launches, tmp_path
):
    repo = _repository(tmp_path)
    daemon.registry.create_spawned(harness="codex", cwd=str(repo), pid="parent", name="Parent")
    daemon.jobs.create(handle="parent#1", caller_id="cli", target_id="parent", kind="send")
    # Deferring after my own running job is legal: it launches once my turn ends.
    reply = await _defer(daemon, repo, after="parent#1", parent_id="parent")

    with pytest.raises(CycleDetected, match="End your turn"):
        await _jobs_await(
            daemon, {"handles": [reply["handle"]], "max_wait": 1, "caller_id": "parent"}
        )

    daemon.jobs.finish("parent#1", state=JobState.DONE, result="done")
    await _launch_all(daemon)
    assert len(launches) == 1  # the same await is no longer a deadlock
    await _jobs_await(daemon, {"handles": [reply["handle"]], "max_wait": 0, "caller_id": "parent"})


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"task_kind": "independent_review"}, "worktree=true"),
        ({"task_kind": "independent_review", "worktree": True, "base_branch": "x"}, "base_branch"),
        ({"resume": "abc"}, "resume"),
        ({"after": "Nobody"}, "neither a job handle"),
        ({"task_kind": "bogus"}, "task_kind must be"),
        ({"prompt": ""}, "needs a prompt"),
    ],
)
async def test_invalid_handoff_requests_are_refused_before_anything_is_reserved(
    daemon, launches, tmp_path, extra, message
):
    repo = _repository(tmp_path)
    _source(daemon, repo)
    before = daemon.store.conn.execute(select(func.count()).select_from(launch_reservations))
    with pytest.raises(BadRequest, match=message):
        await _defer(daemon, repo, **extra)
    assert len(daemon.registry.list()) == 1 and before.scalar() == 0


async def test_killing_the_waiting_placeholder_cancels_its_gate(daemon, launches, tmp_path):
    from theater.daemon.rpc.participants import _kill

    repo = _repository(tmp_path)
    _source(daemon, repo)
    reply = await _defer(daemon, repo)
    handle = reply["handle"]

    await _kill(daemon, {"id": handle, "caller_id": "cli"})

    assert daemon.store.get_handoff_gate(handle).state == "cancelled"
    assert daemon.store.get_job(handle).state == JobState.KILLED
    daemon.jobs.finish("src#1", state=JobState.DONE, result="late")
    await _launch_all(daemon)
    assert launches == [] and _launch_rows(daemon) == 0


async def test_completion_evidence_is_pinned_before_the_terminal_unit_opens(
    daemon, launches, tmp_path
):
    repo = _repository(tmp_path)
    _source(daemon, repo)
    await _defer(daemon, repo)
    head = _git(repo, "rev-parse", "HEAD")

    pinned = await daemon.jobs.prepare_pins("src")
    with daemon.jobs.stage_hashes("src", ()):
        _commit(repo, "after-staging.py")  # a later commit must not leak into the pin
        with daemon.store.write_unit() as unit:
            daemon.jobs.finish(
                "src#1", state=JobState.DONE, result="ok", connection=unit.connection
            )
    daemon.jobs.release_pins(pinned)

    evidence = daemon.store.get_job_evidence("src#1")
    assert (evidence.git.commit_sha, evidence.outcome, evidence.answer) == (head, "done", "ok")

    # Without staging the unit may not fork Git: the evidence says so instead of guessing.
    daemon.jobs.create(handle="src#2", caller_id="cli", target_id="src", kind="send", prompt="more")
    await _defer(daemon, repo, after="src#2")
    with daemon.store.write_unit() as unit:
        daemon.jobs.finish("src#2", state=JobState.DONE, result="ok", connection=unit.connection)
    unstaged = daemon.store.get_job_evidence("src#2")
    assert (unstaged.git.pin_state, unstaged.git.reason) == ("unpinned", "not_staged")


async def test_gc_removes_evidence_and_settled_gates_with_their_jobs(daemon, launches, tmp_path):
    from theater.daemon.gc import _sweep_jobs_and_touch

    repo = _repository(tmp_path)
    _source(daemon, repo)
    reply = await _defer(daemon, repo)
    daemon.jobs.finish("src#1", state=JobState.DONE, result="ok")
    await _launch_all(daemon)
    daemon.jobs.finish(reply["handle"], state=JobState.DONE, result="child done")

    deleted, _ = await _sweep_jobs_and_touch(daemon.store, now() + 10, 100)

    assert deleted >= 2
    assert daemon.store.get_job_evidence("src#1") is None
    assert daemon.store.get_handoff_gate(reply["handle"]) is None


async def test_cancel_job_withdraws_a_waiting_spawn_but_never_a_launching_one(
    daemon, launches, tmp_path
):
    repo = _repository(tmp_path)
    _source(daemon, repo)
    reply = await _defer(daemon, repo)
    handle = reply["handle"]

    outcome = await daemon.controls.cancel_job(handle, requester_id="cli")

    assert outcome.cancelled is True
    assert daemon.store.get_handoff_gate(handle).state == "cancelled"
    assert daemon.store.get_job(handle).state == JobState.KILLED
    assert launches == [] and _launch_rows(daemon) == 0
    again = await daemon.controls.cancel_job(handle, requester_id="cli")
    assert (again.cancelled, again.reason) == (False, "finished")

    # Once the launch has begun there is something to take back; cancel_job no longer applies.
    second = await _defer(daemon, repo, name="Second")
    daemon.store.move_handoff_gate(
        second["handle"], expected=("waiting",), state="opening", updated_at=1.0
    )
    late = await daemon.controls.cancel_job(second["handle"], requester_id="cli")
    assert (late.cancelled, late.reason) == (False, "delivery_uncertain")


async def _until(predicate, *, seconds: float = 3.0) -> None:
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.01)


# ---- H1: a provider that is only away --------------------------------------------------------


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
    repo = _repository(tmp_path)
    _source(daemon, repo)
    reply = await _defer(daemon, repo)
    handle = reply["handle"]

    daemon.jobs.finish("src#1", state=JobState.DONE, result="ok")
    await _until(lambda: daemon.store.get_handoff_gate(handle).error_code == "provider_unavailable")

    gate = daemon.store.get_handoff_gate(handle)
    assert gate.state == "waiting" and daemon.store.get_job(handle).state == JobState.RUNNING
    assert launches == [] and _launch_rows(daemon) == 0

    online["now"] = True
    await _until(lambda: len(launches) == 1)
    await _launch_all(daemon)
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
    repo = _repository(tmp_path)
    _source(daemon, repo)
    handle = (await _defer(daemon, repo))["handle"]
    # The source ended while the daemon was down; the bridge has not reconnected yet.
    with daemon.store.write_unit() as unit:
        unit.connection.execute(
            update(jobs_table).where(jobs_table.c.handle == "src#1").values(state="done")
        )
    daemon.jobs._events.clear()

    await daemon.handoff.recover()
    await _until(lambda: daemon.store.get_handoff_gate(handle).error_code == "provider_unavailable")
    assert daemon.store.get_handoff_gate(handle).state == "waiting" and launches == []

    online["now"] = True
    await _until(lambda: len(launches) == 1)


# ---- H2: the placeholder is already in the tree budget ----------------------------------------


async def test_a_task_admitted_at_the_budget_still_launches_without_double_counting(
    daemon, launches, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        daemon,
        "config",
        dataclasses.replace(
            daemon.config, rails=dataclasses.replace(daemon.config.rails, budget=3)
        ),
    )
    repo = _repository(tmp_path)
    parent = daemon.registry.create_spawned(harness="codex", cwd=str(repo), name="Boss")
    daemon.registry.create_spawned(
        harness="codex", cwd=str(repo), pid="src", name="Writer", parent_id=parent.id
    )
    daemon.jobs.create(handle="src#1", caller_id="cli", target_id="src", kind="send", prompt="x")

    reply = await _defer(daemon, repo, parent_id=parent.id)  # tree: boss, writer, placeholder
    with pytest.raises(BudgetExceeded):
        await _defer(daemon, repo, parent_id=parent.id, name="Another")  # the cap still holds

    daemon.jobs.finish("src#1", state=JobState.DONE, result="ok")
    await _launch_all(daemon)

    assert daemon.store.get_handoff_gate(reply["handle"]).state == "opened"
    assert len(launches) == 1


# ---- M1: naming a participant means its own work ----------------------------------------------


async def test_naming_a_participant_never_selects_a_peers_queued_message(client, daemon):
    from theater.daemon.handoff.resolution import resolve_source

    parent, child, state = await _native_pair(daemon)
    mine = daemon.jobs.create(
        handle=f"{child.id}#own",
        caller_id=parent.id,
        target_id=child.id,
        kind="send",
        prompt="mine",
    )
    state.native_turn_id = "busy"
    stranger = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    queued = await client.call("send", target=child.id, prompt="hi", caller_id=stranger.id)
    assert queued["delivery"] == "queued"

    job, _ = resolve_source(daemon, daemon.registry.get(child.id).name)

    assert job.handle == mine.handle != queued["handle"]
    assert (await client.call("jobs.cancel", handle=queued["handle"], caller_id=stranger.id))[
        "cancelled"
    ]
    job, _ = resolve_source(daemon, daemon.registry.get(child.id).name)
    assert job.handle == mine.handle, "cancelling an undelivered message cannot make it the source"
    daemon.jobs.finish(mine.handle, state=JobState.DONE, result="complete")
    job, _ = resolve_source(daemon, daemon.registry.get(child.id).name)
    assert job.handle == mine.handle


# ---- M2: pinning is one Git process, once per workspace, off the loop -------------------------


def _counting_git(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = gitfacts._git

    def counted(argv, **kwargs):
        calls.append(argv)
        return real(argv, **kwargs)

    monkeypatch.setattr(gitfacts, "_git", counted)
    return calls


def test_one_git_process_pins_commit_branch_and_every_dirty_state(tmp_path, monkeypatch):
    repo = _repository(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
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

    _git(repo, "checkout", "--detach", "-q", head)
    assert gitfacts.pin_git_facts(str(repo)).branch is None

    worktree = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "feature")
    linked = gitfacts.pin_git_facts(str(worktree))
    assert (linked.branch, linked.commit_sha) == ("feature", head)

    empty = tmp_path / "empty"
    subprocess.run(["git", "init", "-q", str(empty)], check=True)
    assert gitfacts.pin_git_facts(str(empty)).reason == "no_commit"
    assert gitfacts.pin_git_facts(str(tmp_path / "nowhere")).reason == "workspace_missing"


async def test_terminal_pins_run_in_a_thread_before_the_unit_and_once_per_workspace(
    daemon, tmp_path, monkeypatch
):
    repo = _repository(tmp_path)
    daemon.registry.create_spawned(harness="codex", cwd=str(repo), pid="src", name="Writer")
    for handle in ("src#1", "src#2"):
        daemon.jobs.create(
            handle=handle, caller_id="cli", target_id="src", kind="send", prompt="work"
        )
    threads: list[int] = []
    real = daemon.jobs.evidence.capture

    def capture(job):
        threads.append(threading.get_ident())
        return real(job)

    monkeypatch.setattr(daemon.jobs.evidence, "capture", capture)
    monkeypatch.setattr(daemon.jobs.evidence, "required", lambda job: True)

    pinned = await daemon.jobs.prepare_pins("src")
    with daemon.jobs.stage_hashes("src", ()), daemon.store.write_unit() as unit:
        for handle in ("src#1", "src#2"):
            daemon.jobs.finish(handle, state=JobState.DONE, result="ok", connection=unit.connection)
    daemon.jobs.release_pins(pinned)

    assert len(threads) == 1 and threads[0] != threading.get_ident()
    head = _git(repo, "rev-parse", "HEAD")
    assert [daemon.store.get_job_evidence(h).git.commit_sha for h in ("src#1", "src#2")] == [
        head,
        head,
    ]


# ---- L1: the GC marks a source crashed behind the listener's back -----------------------------


async def test_a_gate_waiting_on_a_source_the_gc_abandoned_is_woken(daemon, launches, tmp_path):
    repo = _repository(tmp_path)
    _source(daemon, repo)
    handle = (await _defer(daemon, repo))["handle"]
    with daemon.store.write_unit() as unit:
        unit.connection.execute(
            update(jobs_table).where(jobs_table.c.handle == "src#1").values(created_at=1.0)
        )

    assert _sweep_stale_running(daemon.store, 100.0, 100, frozenset()) == 1
    assert daemon.store.get_handoff_gate(handle).state == "waiting", "no listener ran"
    daemon.handoff.wake()
    await _launch_all(daemon)

    assert daemon.store.get_handoff_gate(handle).state == "opened"
    assert "FAILED source" in launches[0].prompt and "abandoned" in launches[0].prompt


# ---- polish: the parent's own messages are not the peers' share -------------------------------


async def test_the_parents_default_send_is_not_counted_against_the_peer_reserve(client, daemon):
    parent, child, state = await _native_pair(daemon)
    state.native_turn_id = "busy"
    strangers = [daemon.registry.create_spawned(harness="vibe", cwd="/tmp") for _ in range(4)]
    for index in range(CONTROL_QUEUE_PEER_MAX_PENDING):
        sender = strangers[index // 8]
        await daemon.controls.send_when_safe(child.id, caller_id=sender.id, prompt=f"p{index}")

    with pytest.raises(Busy, match="reserved for its parent"):
        await daemon.controls.send_when_safe(child.id, caller_id=strangers[3].id, prompt="over")
    job, queued = await daemon.controls.send_when_safe(child.id, caller_id=parent.id, prompt="mine")
    assert queued is True and daemon.store.get_job(job.handle) is not None


# ---- M2 round two: no Git on the loop, none for work that never started -----------------------


@pytest.fixture
async def absent_presence(daemon):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon, "presence", AbsentPresence(), raising=False)
        yield


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
    parent, child, state = await _native_pair(daemon)
    state.native_turn_id = "busy"
    stranger = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    queued = await client.call("send", target=child.id, prompt="hi", caller_id=stranger.id)
    assert queued["delivery"] == "queued"
    repo = Path(daemon.registry.get(child.id).cwd)
    await _defer(daemon, repo, after=queued["handle"], parent_id=parent.id)
    threads = _git_spy(monkeypatch)

    assert daemon.jobs.evidence.required(daemon.store.get_job(queued["handle"])) is False
    await daemon.controls.cancel_job(queued["handle"], requester_id=stranger.id)

    assert threads == [] and daemon.store.get_job_evidence(queued["handle"]) is None


async def test_only_a_job_that_may_have_reached_its_target_can_need_a_pin(
    client, daemon, absent_presence
):
    parent, child, state = await _native_pair(daemon)
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
    repo = _repository(tmp_path)
    _source(daemon, repo)
    await _defer(daemon, repo)
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
    repo = _repository(tmp_path)
    _source(daemon, repo)
    await _defer(daemon, repo)
    threads = _git_spy(monkeypatch)

    daemon.jobs.finish("src#1", state=JobState.DONE, result="ok")  # a sync path, on the loop

    evidence = daemon.store.get_job_evidence("src#1")
    assert (evidence.git.pin_state, evidence.git.reason) == ("unpinned", "not_staged")
    assert threads == []


# ---- completion paths outside an ordinary turn-end batch pin Git in a thread --------------------


async def _review_source(daemon, tmp_path):
    repo = _repository(tmp_path)
    _source(daemon, repo)
    await _defer(daemon, repo, task_kind="independent_review", worktree=True, prompt="Review it")
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


# ---- H1 round two: only an absent provider is waited for -----------------------------------------


async def test_a_provider_that_does_not_exist_is_refused_where_it_is_asked_for(
    daemon, launches, tmp_path
):
    from theater.daemon.terminals import ProviderUnavailable

    repo = _repository(tmp_path)
    _source(daemon, repo)

    with pytest.raises(ProviderUnavailable, match="not_registered"):
        await _defer(daemon, repo, provider="no-such-provider")
    assert len(daemon.registry.list()) == 1 and daemon.store.handoff_gates(("waiting",)) == []


@pytest.mark.parametrize("reason", ["not_registered", "missing_terminal_create_capability"])
async def test_a_permanent_provider_refusal_ends_the_gate_instead_of_retrying(
    daemon, launches, tmp_path, monkeypatch, reason
):
    from theater.daemon.spawning.provider_launch import ParticipantLaunchService
    from theater.daemon.terminals import ProviderUnavailable

    repo = _repository(tmp_path)
    _source(daemon, repo)
    handle = (await _defer(daemon, repo))["handle"]

    async def refuse(self, **_):
        raise ProviderUnavailable("provider-tmux", reason)

    monkeypatch.setattr(ParticipantLaunchService, "spawn", refuse)
    await _end(daemon, state=JobState.DONE, result="ok")
    await daemon.handoff.settle()

    gate = daemon.store.get_handoff_gate(handle)
    job = daemon.store.get_job(handle)
    assert (gate.state, job.state, job.error_code) == (
        "failed",
        JobState.CRASHED,
        "handoff_launch_refused",
    )
    assert reason in gate.error and not daemon.handoff._retrying
