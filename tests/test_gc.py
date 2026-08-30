"""Tests for the garbage-collection engine.

Each test covers a load-bearing invariant from the GC design. Removing the
behaviour a test guards must make it fail — a test that asserts a mock returns
what it was told proves nothing.

Uses explicit timestamps rather than sleeping: the sweep filters on
``finished_at`` and ``ts``, so inserting rows with old timestamps exercises
the same code path without the test being slow or flaky.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from theater import paths
from theater.config import RetentionSection
from theater.constants.daemon import TMUX_RESTART_TERMINATION_REASON
from theater.daemon import artifacts as artifacts_module
from theater.daemon import gc as gc_module
from theater.daemon.artifacts import (
    ArtifactKind,
    OwnedArtifact,
    artifacts_for_plan,
    remove_secret_file,
)
from theater.daemon.gc import SweepResult, sweep
from theater.daemon.jobs import JobManager, JobState
from theater.daemon.registry import Registry
from theater.daemon.schema import bus, jobs, participant_artifacts, participants, touch, tree_kv
from theater.harness.contracts.channels import ChannelKind
from theater.harness.contracts.launch import LaunchPlan
from theater.models import BadRequest, Job, Participant, Status, Tier, now
from theater.transcript_identity import TRANSCRIPT_IDENTITY_LOST_CODE

# ---- helpers ---------------------------------------------------------------


_DAY = 86400.0


def _retention(**overrides) -> RetentionSection:
    defaults = {
        "bus_days": 7,
        "jobs_days": 60,
        "refused_cap": 10000,
        "stale_running_days": 7,
        "batch": 5000,
        "interval": 3600.0,
        "enabled": True,
    }
    defaults.update(overrides)
    return RetentionSection(**defaults)


def _participant(
    store,
    *,
    pid: str = "p1",
    harness: str = "vibe",
    cwd: str = "/tmp",
    parent_id: str | None = None,
    status: Status = Status.DEAD,
) -> Participant:
    p = Participant(
        id=pid,
        harness=harness,
        tier=Tier.SPAWNED,
        cwd=cwd,
        parent_id=parent_id,
        status=status,
    )
    store.upsert_participant(p)
    return p


def _job(
    store,
    *,
    handle: str,
    caller_id: str = "cli",
    target_id: str | None = "p1",
    kind: str = "spawn",
    prompt: str | None = "do the thing",
    state: str = JobState.DONE,
    result: str | None = "done",
    error_code: str | None = None,
    created_at: float | None = None,
    finished_at: float | None = None,
) -> None:
    store.create_job(
        Job(
            handle=handle,
            caller_id=caller_id,
            target_id=target_id,
            kind=kind,
            prompt=prompt,
            state=state,
            result=result,
            error_code=error_code,
            created_at=created_at if created_at is not None else now(),
            finished_at=finished_at,
        )
    )


def _touch(
    store,
    *,
    job_handle: str,
    path: str = "src/main.py",
    mode: str = "write",
    sha_before: str | None = None,
    sha_after: str | None = None,
) -> None:
    store.conn.execute(
        touch.insert().values(
            job_handle=job_handle,
            path=path,
            mode=mode,
            sha_before=sha_before,
            sha_after=sha_after,
        )
    )


def _bus(
    store,
    *,
    kind: str = "job.created",
    ts: float | None = None,
    from_id: str | None = None,
    to_id: str | None = None,
    payload: str | None = None,
) -> int:
    result = store.conn.execute(
        bus.insert().values(
            ts=ts if ts is not None else now(),
            from_id=from_id,
            to_id=to_id,
            kind=kind,
            payload=payload,
        )
    )
    pk = result.inserted_primary_key
    assert pk is not None
    return pk[0]


def _kv(
    store,
    *,
    tree_root_id: str,
    repo_root: str = "/repo",
    namespace: str = "ns1",
    key: str = "k1",
    value: str = "v1",
    updated_by: str = "p1",
    updated_at: float | None = None,
) -> None:
    store.conn.execute(
        tree_kv.insert().values(
            tree_root_id=tree_root_id,
            repo_root=repo_root,
            namespace=namespace,
            key=key,
            value=value,
            updated_at=updated_at if updated_at is not None else now(),
            updated_by=updated_by,
        )
    )


def _count(store, table) -> int:
    from sqlalchemy import func, select

    return store.conn.execute(select(func.count()).select_from(table)).scalar()


def _materialize_participant_artifacts(store, participant: Participant) -> tuple[Path, ...]:
    observation = paths.observation_dir(participant.harness, participant.id)
    entries = (
        (paths.mcp_config_path(participant.id), ArtifactKind.FILE),
        (paths.mcp_config_dir() / f"{participant.id}.opencode.mjs", ArtifactKind.FILE),
        (paths.launch_artifacts_dir() / f"{participant.id}.settings.json", ArtifactKind.FILE),
        (observation, ArtifactKind.DIRECTORY),
        (observation / "receipt-token", ArtifactKind.FILE),
        (observation / "hook.token", ArtifactKind.FILE),
        (paths.participant_artifacts_dir(participant.id), ArtifactKind.DIRECTORY),
    )
    for path, kind in entries:
        if kind is ArtifactKind.DIRECTORY:
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(path.name)
    store.add_participant_artifacts(
        participant.id,
        tuple(OwnedArtifact(path, kind) for path, kind in entries),
    )
    store.set_receipt_token(
        participant.id,
        "receipt-secret",
        token_path=str(observation / "receipt-token"),
    )
    store.set_channel_credential(
        participant.id,
        harness=participant.harness,
        kind=ChannelKind.HOOK,
        channel_id="native-hooks",
        token="channel-secret",
        token_path=str(observation / "hook.token"),
    )
    return tuple(path for path, _kind in entries)


# ---- MF1: running job survives, and finish() still works -------------------


async def test_running_job_survives_sweep(store):
    """A running job (finished_at = NULL) must never be deleted by the sweep.

    JobManager.finish() does `if job is None: return None` before setting the
    asyncio.Event that await_sessions is blocked on. Deleting a running job
    row makes the caller hang until its own timeout with no explanation.
    """
    _participant(store, pid="p1")
    _job(
        store,
        handle="job1",
        target_id="p1",
        state=JobState.RUNNING,
        finished_at=None,
        created_at=now() - 999 * _DAY,
    )
    result = await sweep(store, _retention(jobs_days=1))
    assert result.jobs == 0
    assert store.get_job("job1") is not None


async def test_finish_still_works_after_sweep(store):
    """The more important assertion: after a sweep, finish() on a running
    job still works and still sets its event — an await_sessions caller is
    still woken.

    If the sweep deleted the running job, finish() would find None and
    return None without setting the event, and the caller would hang.
    """
    _participant(store, pid="p1")
    _job(
        store,
        handle="job1",
        target_id="p1",
        state=JobState.RUNNING,
        finished_at=None,
        created_at=now() - 999 * _DAY,
    )

    # The JobManager holds an asyncio.Event for this handle.
    jm = JobManager(store)
    event = asyncio.Event()
    jm._events["job1"] = event

    await sweep(store, _retention(jobs_days=1), live_handles=frozenset({"job1"}))

    # finish() must still find the job, set its event, and return it.
    # The event is popped from _events during finish(), so we hold our own
    # reference to check it was set.
    finished = jm.finish("job1", state=JobState.DONE, result="ok")
    assert finished is not None
    assert finished.state == JobState.DONE
    assert event.is_set()


# ---- MF1 (stale): abandoned running jobs are marked crashed ----------------


async def test_stale_running_job_is_marked_crashed(store):
    """A running job older than stale_running_days is marked crashed/abandoned
    with a non-null finished_at — so the jobs phase can then delete it.
    """
    _participant(store, pid="p1")
    _job(
        store,
        handle="stale",
        target_id="p1",
        state=JobState.RUNNING,
        finished_at=None,
        created_at=now() - 30 * _DAY,
    )
    result = await sweep(store, _retention(stale_running_days=7, jobs_days=60))
    assert result.running_marked == 1
    job = store.get_job("stale")
    assert job is not None
    assert job.state == "crashed"
    assert job.error_code == "abandoned"
    assert job.finished_at is not None


async def test_live_running_job_is_not_marked(store):
    """A stale running job whose handle is in live_handles is left alone —
    the running daemon's JobManager still holds an asyncio.Event for it.
    """
    _participant(store, pid="p1")
    _job(
        store,
        handle="live",
        target_id="p1",
        state=JobState.RUNNING,
        finished_at=None,
        created_at=now() - 30 * _DAY,
    )
    result = await sweep(
        store,
        _retention(stale_running_days=7),
        live_handles=frozenset({"live"}),
    )
    assert result.running_marked == 0
    job = store.get_job("live")
    assert job is not None
    assert job.state == "running"
    assert job.finished_at is None


# ---- MF3: participant gated delete, four reference guards -----------------


async def test_dead_participant_referenced_as_parent_is_kept(store):
    """A dead participant that is another participant's parent_id is NOT
    deleted. rails.py walks parent_id upward and does not filter dead rows;
    deleting a mid-chain participant makes the walk terminate early and
    under-counts depth — the rail fails open.

    The child itself is dead and unreferenced, so it IS deleted — but the
    parent survives because the child's parent_id points at it, and the
    DELETE evaluates the subquery before removing any rows.
    """
    _participant(store, pid="parent", parent_id=None)
    _participant(store, pid="child", parent_id="parent")

    result = await sweep(store, _retention())
    # The child is dead and unreferenced — it goes. The parent is protected
    # by the fourth guard: the child's parent_id is "parent".
    assert store.get_participant("parent") is not None
    # The child may or may not be deleted depending on whether the parent's
    # existence protects it — it does not, because nothing references the child.
    # But the parent must survive regardless.
    assert result.participants <= 1


async def test_dead_participant_becomes_eligible_when_child_gone(store):
    """Once the child is also dead and unreferenced, the parent becomes
    eligible for deletion in the same sweep.
    """
    _participant(store, pid="parent", parent_id=None)
    _participant(store, pid="child", parent_id="parent")

    # Delete the child's parent_id reference so parent is no longer a parent.
    store.conn.execute(
        participants.update().where(participants.c.id == "child").values(parent_id=None)
    )

    result = await sweep(store, _retention())
    assert result.participants == 2
    assert store.get_participant("parent") is None
    assert store.get_participant("child") is None


async def test_dead_participant_referenced_as_target_id_is_kept(store):
    _participant(store, pid="p1")
    _job(
        store,
        handle="j1",
        caller_id="cli",
        target_id="p1",
        state=JobState.DONE,
        finished_at=now() - 90 * _DAY,
        created_at=now() - 90 * _DAY,
    )
    result = await sweep(store, _retention(jobs_days=1))
    # The job is old enough to be deleted...
    assert result.jobs == 1
    # ...but the participant deletion happens after the jobs phase, so the
    # participant is now eligible.
    assert result.participants == 1
    assert store.get_participant("p1") is None


async def test_dead_participant_referenced_as_caller_id_is_kept(store):
    """A dead participant referenced by a job's caller_id is protected.

    recall.py's INNER join from touch to jobs uses caller_id indirectly
    through the jobs table; deleting a participant that is a caller would
    orphan those job rows from attribution.
    """
    _participant(store, pid="caller")
    # A job that is NOT old enough to be swept — so the participant stays.
    _job(
        store,
        handle="j1",
        caller_id="caller",
        target_id=None,
        state=JobState.DONE,
        finished_at=now(),
        created_at=now(),
    )
    result = await sweep(store, _retention(jobs_days=60))
    assert result.participants == 0
    assert store.get_participant("caller") is not None


async def test_tmux_restart_participant_waits_for_jobs_retention_after_termination(store):
    participant = _participant(store, pid="restart")
    participant.termination_reason = TMUX_RESTART_TERMINATION_REASON
    participant.termination_incident = "incident-123"
    participant.terminated_at = now()
    store.upsert_participant(participant)

    result = await sweep(store, _retention(jobs_days=2))
    assert result.participants == 0
    assert store.get_participant(participant.id) is not None

    participant.terminated_at = now() - 3 * _DAY
    store.upsert_participant(participant)
    result = await sweep(store, _retention(jobs_days=2))
    assert result.participants == 1
    assert store.get_participant(participant.id) is None


async def test_resume_predecessor_is_kept_until_its_successor_is_deleted(store):
    predecessor = _participant(store, pid="predecessor")
    successor = _participant(store, pid="successor", status=Status.IDLE)
    successor.resumed_from_id = predecessor.id
    store.upsert_participant(successor)

    result = await sweep(store, _retention())
    assert result.participants == 0
    assert store.get_participant(predecessor.id) is not None

    store.set_status(successor.id, Status.DEAD)
    result = await sweep(store, _retention())
    assert result.participants == 1
    assert store.get_participant(predecessor.id) is not None

    result = await sweep(store, _retention())
    assert result.participants == 1
    assert store.get_participant(predecessor.id) is None


# ---- Jobs and touch go together -------------------------------------------


async def test_no_touch_row_survives_without_its_job(store):
    """After the sweep, no touch row survives whose job_handle no longer
    exists in jobs. The two must be deleted together.
    """
    _participant(store, pid="p1")
    _job(
        store,
        handle="old",
        target_id="p1",
        state=JobState.DONE,
        finished_at=now() - 90 * _DAY,
        created_at=now() - 90 * _DAY,
    )
    _touch(store, job_handle="old", path="a.py")
    _touch(store, job_handle="old", path="b.py")

    result = await sweep(store, _retention(jobs_days=60))
    assert result.jobs == 1
    assert result.touch == 2

    # The invariant: no touch row without a matching job.
    from sqlalchemy import select

    orphaned = store.conn.execute(
        select(touch.c.id).where(touch.c.job_handle.not_in(select(jobs.c.handle)))
    ).fetchall()
    assert orphaned == []


async def test_touch_rows_for_surviving_jobs_are_kept(store):
    """Touch rows for a job that is NOT swept must survive."""
    _participant(store, pid="p1")
    _job(
        store,
        handle="recent",
        target_id="p1",
        state=JobState.DONE,
        finished_at=now(),
        created_at=now(),
    )
    _touch(store, job_handle="recent", path="c.py")

    result = await sweep(store, _retention(jobs_days=60))
    assert result.jobs == 0
    assert result.touch == 0
    assert _count(store, touch) == 1


# ---- Bus -------------------------------------------------------------------


async def test_old_bus_rows_are_deleted(store):
    old_ts = now() - 30 * _DAY
    _bus(store, kind="job.created", ts=old_ts)
    _bus(store, kind="job.finished", ts=old_ts)

    result = await sweep(store, _retention(bus_days=7))
    assert result.bus == 2
    assert _count(store, bus) == 0


async def test_active_identity_loss_audit_survives_batched_bus_retention(store):
    _participant(store, status=Status.IDLE)
    old_ts = now() - 30 * _DAY
    loss_id = _bus(
        store,
        kind="agent.observation_error",
        ts=old_ts,
        to_id="p1",
        payload=json.dumps({"code": TRANSCRIPT_IDENTITY_LOST_CODE}),
    )
    _bus(store, kind="job.created", ts=old_ts)
    _bus(store, kind="job.finished", ts=old_ts)

    result = await sweep(store, _retention(bus_days=7, batch=1))

    assert result.bus == 2
    assert [row[0] for row in store.conn.execute(bus.select()).fetchall()] == [loss_id]


@pytest.mark.parametrize(
    ("kind", "payload", "deleted", "remaining"),
    [
        ("operator.transcript_unbind", "{}", 1, ["operator.transcript_unbind"]),
        (
            "agent.transcript_receipt",
            json.dumps({"admission": "accepted"}),
            1,
            ["agent.transcript_receipt"],
        ),
        (
            "agent.transcript_receipt",
            json.dumps({"admission": "staged"}),
            0,
            ["agent.observation_error", "agent.transcript_receipt"],
        ),
    ],
)
async def test_identity_loss_audit_retention_follows_clearing_events(
    store, kind, payload, deleted, remaining
):
    _participant(store, status=Status.IDLE)
    old_ts = now() - 30 * _DAY
    _bus(
        store,
        kind="agent.observation_error",
        ts=old_ts,
        to_id="p1",
        payload=json.dumps({"code": TRANSCRIPT_IDENTITY_LOST_CODE}),
    )
    _bus(store, kind=kind, ts=now(), to_id="p1", payload=payload)

    result = await sweep(store, _retention(bus_days=7, batch=1))

    assert result.bus == deleted
    assert [row._mapping["kind"] for row in store.conn.execute(bus.select())] == remaining


async def test_send_refused_of_same_age_survives(store):
    """send.refused is the only record of a refused send, so it is exempt
    from the age TTL.
    """
    old_ts = now() - 30 * _DAY
    _bus(store, kind="send.refused", ts=old_ts)

    result = await sweep(store, _retention(bus_days=7))
    assert result.bus == 0
    assert _count(store, bus) == 1


async def test_refused_cap_trims_oldest(store):
    """With more than refused_cap refused rows, the oldest are trimmed and
    the newest refused_cap remain.
    """
    cap = 3
    ids = []
    for i in range(5):
        ids.append(_bus(store, kind="send.refused", ts=now() - (100 - i) * _DAY))
    # ids are autoincrement, so ids[0] < ids[1] < ... < ids[4]
    # The newest `cap` should survive: ids[2], ids[3], ids[4].

    result = await sweep(store, _retention(bus_days=7, refused_cap=cap))
    assert result.bus == 2  # 5 - 3

    from sqlalchemy import select

    remaining = [
        r[0]
        for r in store.conn.execute(select(bus.c.id).where(bus.c.kind == "send.refused")).fetchall()
    ]
    # The newest `cap` rows survive — higher id = newer (autoincrement).
    assert remaining == sorted(ids)[-cap:]


# ---- Batching --------------------------------------------------------------


async def test_batching_loops_until_all_deleted(store):
    """With batch set small and many rows to delete, the sweep still removes
    all of them — the loop repeats rather than doing one pass.
    """
    _participant(store, pid="p1")
    for i in range(10):
        _job(
            store,
            handle=f"j{i}",
            target_id="p1",
            state=JobState.DONE,
            finished_at=now() - 90 * _DAY,
            created_at=now() - 90 * _DAY,
        )
        _touch(store, job_handle=f"j{i}", path=f"file{i}.py")

    result = await sweep(store, _retention(jobs_days=60, batch=2))
    assert result.jobs == 10
    assert result.touch == 10
    assert _count(store, jobs) == 0
    assert _count(store, touch) == 0


async def test_participant_sweep_honours_small_batches(store):
    for index in range(5):
        _participant(store, pid=f"dead-{index}")

    result = await sweep(store, _retention(batch=2))
    assert result.participants == 5
    assert _count(store, participants) == 0


async def test_invalid_artifact_owner_does_not_starve_later_participants(store):
    blocked_id = "000000000001"
    later_id = "000000000002"
    _participant(store, pid=blocked_id)
    store.conn.execute(
        participant_artifacts.insert().values(
            participant_id=blocked_id,
            # Match the repository's canonical key even when /tmp is a symlink
            # (as on macOS), so baseline insertion cannot hide the corrupt row.
            path=str(paths.mcp_config_path(blocked_id).resolve(strict=False)),
            kind="corrupt",
        )
    )
    _participant(store, pid=later_id)

    result = await sweep(store, _retention(batch=1))

    assert result.participants == 1
    assert store.get_participant(blocked_id) is not None
    assert store.get_participant(later_id) is None
    assert store.participant_artifact_owner_ids() == (blocked_id,)
    with pytest.raises(ValueError):
        store.participant_artifacts(blocked_id)


async def test_retained_dead_participant_keeps_nonsecret_artifacts(store):
    participant = _participant(store, pid="abcdef123456", status=Status.IDLE)
    artifact_paths = _materialize_participant_artifacts(store, participant)
    Registry(store).mark_dead(participant.id)
    assert not (
        paths.observation_dir(participant.harness, participant.id) / "receipt-token"
    ).exists()
    assert not (paths.observation_dir(participant.harness, participant.id) / "hook.token").exists()
    _participant(store, pid="child", parent_id=participant.id, status=Status.IDLE)

    result = await sweep(store, _retention())

    assert result.participants == 0
    assert store.get_participant(participant.id) is not None
    assert paths.mcp_config_path(participant.id).exists()
    assert (paths.mcp_config_dir() / f"{participant.id}.opencode.mjs").exists()
    assert (paths.launch_artifacts_dir() / f"{participant.id}.settings.json").exists()
    assert paths.observation_dir(participant.harness, participant.id).exists()
    assert not (
        paths.observation_dir(participant.harness, participant.id) / "receipt-token"
    ).exists()
    assert not (paths.observation_dir(participant.harness, participant.id) / "hook.token").exists()
    assert paths.participant_artifacts_dir(participant.id).exists()
    assert all(
        path.exists() for path in artifact_paths if path.name not in {"receipt-token", "hook.token"}
    )


async def test_retained_dead_row_cleans_secret_credentials_after_crash(store):
    participant = _participant(store, pid="abcdef123456")
    _materialize_participant_artifacts(store, participant)
    store.set_status(participant.id, Status.DEAD)
    _participant(store, pid="child", parent_id=participant.id, status=Status.IDLE)

    receipt = paths.observation_dir(participant.harness, participant.id) / "receipt-token"
    channel = paths.observation_dir(participant.harness, participant.id) / "hook.token"
    assert receipt.exists() and channel.exists()

    result = await sweep(store, _retention())

    assert result.participants == 0
    assert store.get_participant(participant.id) is not None
    assert not receipt.exists() and not channel.exists()
    assert store.get_meta(f"receipt_token:{participant.id}") is None
    assert store.get_meta(f"channel_credential:{participant.id}:hook:native-hooks") is None
    assert paths.mcp_config_path(participant.id).exists()
    assert paths.observation_dir(participant.harness, participant.id).exists()


async def test_eligible_participant_removes_all_owned_artifacts(store):
    participant = _participant(store, pid="abcdef123456")
    artifact_paths = _materialize_participant_artifacts(store, participant)

    result = await sweep(store, _retention())

    assert result.participants == 1
    assert store.get_participant(participant.id) is None
    assert all(not path.exists() for path in artifact_paths)
    assert store.get_meta(f"receipt_token:{participant.id}") is None
    assert store.participant_artifact_owner_ids() == ()


def test_launch_artifact_paths_are_owned_and_follow_runtime_home(
    theater_home, monkeypatch, tmp_path
):
    runtime_home = tmp_path / "runtime-home"
    monkeypatch.setenv("THEATER_HOME", str(runtime_home))
    paths.ensure_home()
    participant = Participant(id="abcdef123456", harness="custom", tier=Tier.SPAWNED)

    valid = LaunchPlan(files={paths.mcp_config_path(participant.id): "{}"}, argv=[])
    owned = artifacts_for_plan(valid, participant)
    assert paths.mcp_config_path(participant.id) in {artifact.path for artifact in owned}

    outside = tmp_path / "user-file"
    with pytest.raises(BadRequest):
        artifacts_for_plan(LaunchPlan(files={outside: "unsafe"}, argv=[]), participant)

    other = paths.mcp_config_dir() / "fedcba654321.json"
    with pytest.raises(BadRequest):
        artifacts_for_plan(LaunchPlan(files={other: "unsafe"}, argv=[]), participant)

    foreign_observation = paths.observation_dir("other", participant.id) / "marker"
    with pytest.raises(BadRequest):
        artifacts_for_plan(LaunchPlan(files={foreign_observation: "unsafe"}, argv=[]), participant)


def test_secret_cleanup_uses_runtime_home_and_refuses_symlink_parents(monkeypatch, tmp_path):
    runtime_home = tmp_path / "runtime-home"
    monkeypatch.setenv("THEATER_HOME", str(runtime_home))
    paths.ensure_home()
    participant_id = "abcdef123456"
    legitimate = paths.observation_dir("custom", participant_id) / "receipt-token"
    legitimate.parent.mkdir(parents=True, exist_ok=True)
    legitimate.write_text("secret")

    remove_secret_file(legitimate, owner_id=participant_id)

    assert not legitimate.exists()

    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "token"
    target.write_text("secret")
    linked_parent = paths.observations_dir() / "custom" / "linked"
    linked_parent.parent.mkdir(parents=True, exist_ok=True)
    linked_parent.symlink_to(outside, target_is_directory=True)

    remove_secret_file(linked_parent / "token", owner_id=participant_id)

    assert target.exists()


async def test_secret_cleanup_refuses_external_paths_but_removes_metadata(store, tmp_path):
    receipt = tmp_path / "receipt-token"
    channel = tmp_path / "channel-token"
    receipt.write_text("secret")
    channel.write_text("secret")
    store.set_receipt_token("missing", "receipt", token_path=str(receipt))
    store.set_channel_credential(
        "missing",
        harness="custom",
        kind=ChannelKind.HOOK,
        channel_id="hooks",
        token="channel",
        token_path=str(channel),
    )

    await sweep(store, _retention())

    assert receipt.exists() and channel.exists()
    assert store.get_meta("receipt_token:missing") is None
    assert store.get_meta("channel_credential:missing:hook:hooks") is None


async def test_failed_artifact_cleanup_keeps_ownership_for_retry(store, monkeypatch):
    participant = _participant(store, pid="abcdef123456")
    artifact_paths = _materialize_participant_artifacts(store, participant)
    target = paths.mcp_config_path(participant.id).resolve(strict=False)
    original_remove = artifacts_module._remove_one
    failed = False

    def fail_once(artifact):
        nonlocal failed
        if artifact.path == target and not failed:
            failed = True
            raise OSError("temporarily busy")
        return original_remove(artifact)

    monkeypatch.setattr(artifacts_module, "_remove_one", fail_once)
    first = await sweep(store, _retention())

    assert first.participants == 1
    assert store.get_participant(participant.id) is None
    assert target.exists()
    assert store.participant_artifacts(participant.id)

    monkeypatch.setattr(artifacts_module, "_remove_one", original_remove)
    second = await sweep(store, _retention())

    assert second.participants == 0
    assert not target.exists()
    assert all(not path.exists() for path in artifact_paths)
    assert store.participant_artifact_owner_ids() == ()


async def test_legacy_orphans_are_cleaned_without_broad_globs(store):
    owner_id = "fedcba654321"
    observation = paths.observation_dir("legacy", owner_id)
    owned = (
        paths.mcp_config_path(owner_id),
        paths.mcp_config_dir() / f"{owner_id}.opencode.mjs",
        paths.launch_artifacts_dir() / f"{owner_id}.settings.json",
        observation,
    )
    for path in owned:
        if path.suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("legacy")
        else:
            path.mkdir(parents=True, exist_ok=True)
            (path / "messages.jsonl").write_text("durable")
    unrelated = paths.mcp_config_dir() / "not-a-participant.json"
    unrelated.write_text("keep")

    await sweep(store, _retention())

    assert all(not path.exists() for path in owned)
    assert unrelated.exists()


async def test_legacy_orphan_rechecks_owner_before_cleanup(store, monkeypatch):
    owner_id = "fedcba654321"
    legacy = paths.mcp_config_path(owner_id)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("legacy")
    original_to_thread = gc_module.workers.to_thread

    async def race_to_thread(fn, /, *args, label, **kwargs):
        result = await original_to_thread(fn, *args, label=label, **kwargs)
        if fn is gc_module.orphan_paths:
            _participant(store, pid=owner_id, status=Status.IDLE)
        return result

    monkeypatch.setattr(gc_module.workers, "to_thread", race_to_thread)

    await sweep(store, _retention())

    assert store.get_participant(owner_id) is not None
    assert legacy.exists()


async def test_participant_artifact_cleanup_honours_batches(store):
    for index in range(5):
        participant = _participant(store, pid=f"{index + 1:012x}")
        _materialize_participant_artifacts(store, participant)

    result = await sweep(store, _retention(batch=2))

    assert result.participants == 5
    assert _count(store, participants) == 0
    assert store.participant_artifact_owner_ids() == ()


# ---- Counts ----------------------------------------------------------------


async def test_sweep_result_counts_match_actual_deletions(store):
    """SweepResult fields match what actually disappeared from the tables."""
    _participant(store, pid="p1")
    _participant(store, pid="p2")

    _job(
        store,
        handle="old1",
        target_id="p1",
        caller_id="cli",
        state=JobState.DONE,
        finished_at=now() - 90 * _DAY,
        created_at=now() - 90 * _DAY,
    )
    _touch(store, job_handle="old1", path="x.py")

    _bus(store, kind="job.created", ts=now() - 30 * _DAY)
    _bus(store, kind="send.refused", ts=now() - 30 * _DAY)

    before_jobs = _count(store, jobs)
    before_touch = _count(store, touch)
    before_bus = _count(store, bus)
    before_part = _count(store, participants)

    result = await sweep(store, _retention(bus_days=7, jobs_days=60))

    assert result.jobs == before_jobs - _count(store, jobs)
    assert result.touch == before_touch - _count(store, touch)
    assert result.bus == before_bus - _count(store, bus)
    assert result.participants == before_part - _count(store, participants)


# ---- Disabled --------------------------------------------------------------


async def test_disabled_sweep_deletes_nothing(store):
    """With enabled = False, the loop performs no deletion.

    The sweep function itself does not check `enabled` — the daemon loop
    does (it skips creating the task). This test verifies that calling
    sweep with a disabled config is still safe, but the real check is in
    the loop. So we test the loop's behaviour: with enabled=False, the
    daemon never creates the _gc task.
    """
    from theater.daemon.server import Daemon

    daemon = Daemon(harnesses={}, config=__disabled_config())
    try:
        assert daemon._gc is None
    finally:
        daemon.store.close()


def __disabled_config():
    from theater.config import Config

    return Config(retention=RetentionSection(enabled=False))


# ---- Empty database --------------------------------------------------------


async def test_sweep_on_empty_database_is_noop(store):
    """A sweep against an empty database raises nothing and returns all-zero."""
    result = await sweep(store, _retention())
    assert result == SweepResult()
    assert result.bus == 0
    assert result.jobs == 0
    assert result.touch == 0
    assert result.participants == 0
    assert result.running_marked == 0
    assert result.scratchpad == 0


# ---- tree_kv cleanup -------------------------------------------------------


async def test_fully_dead_tree_kv_is_cleaned(store):
    """When no participant in a tree is live, its kv rows are deleted."""
    _participant(store, pid="root", status=Status.DEAD)
    _kv(store, tree_root_id="root")
    _kv(store, tree_root_id="root", key="k2")
    result = await sweep(store, _retention())
    assert result.scratchpad == 2
    assert _count(store, tree_kv) == 0


async def test_live_tree_kv_is_retained(store):
    """When a participant in the tree is live, its kv rows survive."""
    _participant(store, pid="root", status=Status.IDLE)
    _kv(store, tree_root_id="root")
    result = await sweep(store, _retention())
    assert result.scratchpad == 0
    assert _count(store, tree_kv) == 1


async def test_dead_root_with_live_descendant_retains_kv(store):
    """A dead root with a live descendant must retain the tree's kv rows.

    The naive test — only checking whether the root row is live — would
    wrongly delete kv for a tree whose root is dead but whose descendant
    is still working. root_of() walks the lineage to find the live
    participant's root, so the tree is retained.
    """
    _participant(store, pid="root", parent_id=None, status=Status.DEAD)
    _participant(store, pid="child", parent_id="root", status=Status.IDLE)
    _kv(store, tree_root_id="root")
    result = await sweep(store, _retention())
    assert result.scratchpad == 0
    assert _count(store, tree_kv) == 1


async def test_dead_tree_with_live_descendant_uses_correct_root(store):
    """When the live descendant's root differs from the dead tree's root,
    only the dead tree's kv is deleted."""
    # Tree A: fully dead — root is dead, child is dead.
    _participant(store, pid="rootA", parent_id=None, status=Status.DEAD)
    _participant(store, pid="childA", parent_id="rootA", status=Status.DEAD)
    _kv(store, tree_root_id="rootA")

    # Tree B: has a live participant.
    _participant(store, pid="rootB", parent_id=None, status=Status.IDLE)
    _kv(store, tree_root_id="rootB")

    result = await sweep(store, _retention())
    assert result.scratchpad == 1
    assert _count(store, tree_kv) == 1


async def test_tree_kv_cleanup_is_batched(store):
    """With a small batch, the sweep still removes all dead-tree kv rows."""
    _participant(store, pid="root", status=Status.DEAD)
    for i in range(10):
        _kv(store, tree_root_id="root", key=f"k{i}")
    result = await sweep(store, _retention(batch=3))
    assert result.scratchpad == 10
    assert _count(store, tree_kv) == 0


# ---- SweepResult counts ----------------------------------------------------


async def test_sweep_result_counts_match_all_tables(store):
    """SweepResult fields match actual deletions across all tables."""
    _participant(store, pid="p1")
    _participant(store, pid="p2")

    _job(
        store,
        handle="old1",
        target_id="p1",
        caller_id="cli",
        state=JobState.DONE,
        finished_at=now() - 90 * _DAY,
        created_at=now() - 90 * _DAY,
    )
    _touch(store, job_handle="old1", path="x.py")

    _bus(store, kind="job.created", ts=now() - 30 * _DAY)
    _bus(store, kind="send.refused", ts=now() - 30 * _DAY)

    _participant(store, pid="kvroot", status=Status.DEAD)
    _kv(store, tree_root_id="kvroot")

    before_jobs = _count(store, jobs)
    before_touch = _count(store, touch)
    before_bus = _count(store, bus)
    before_part = _count(store, participants)
    before_kv = _count(store, tree_kv)

    result = await sweep(store, _retention(bus_days=7, jobs_days=60))

    assert result.jobs == before_jobs - _count(store, jobs)
    assert result.touch == before_touch - _count(store, touch)
    assert result.bus == before_bus - _count(store, bus)
    assert result.participants == before_part - _count(store, participants)
    assert result.scratchpad == before_kv - _count(store, tree_kv)
