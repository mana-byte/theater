"""Durable ``after`` handoffs: deferred spawn, completion evidence, review isolation, awaits."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import func, select

from theater.daemon.operations import OperationOutcome
from theater.daemon.rails import CycleDetected
from theater.daemon.rpc.jobs import _jobs_await
from theater.daemon.rpc.spawning import _spawn
from theater.daemon.schema import launch_reservations
from theater.daemon.spawning.models import Reservation
from theater.harness.base import LaunchPlan
from theater.models import BadRequest, JobState, ProviderRecord, now

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
    daemon.jobs.finish("src#1", state=JobState.DONE, result="PLAN: do X")
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
    daemon.jobs.finish("src#1", state=JobState.DONE, result="SECRET-CONCLUSION: ship it")
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
    daemon.jobs.finish("src#1", state=JobState.DONE, result="done")
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

    with daemon.jobs.stage_hashes("src", ()):
        _commit(repo, "after-staging.py")  # a later commit must not leak into the pin
        with daemon.store.write_unit() as unit:
            daemon.jobs.finish(
                "src#1", state=JobState.DONE, result="ok", connection=unit.connection
            )

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
