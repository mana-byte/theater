"""The ``theater gc`` command and its RPC.

Tests the front door, not the engine: every deletion query already exists in
``gc.py`` and is tested in ``test_gc.py``. Here we verify that the RPC returns
the documented keys with the right counts, that ``live_handles`` protects a
live job, that ``--vacuum`` runs after the sweep, that ``retention.enabled``
being false does not stop a manual sweep, and that the human-readable output
communicates the one thing that matters — that deleting rows does not shrink
the file.
"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import func, select

from theater import cli
from theater.daemon.jobs import JobState
from theater.daemon.schema import bus, jobs, participants, touch, tree_kv
from theater.models import Job, Participant, Status, Tier, now

_DAY = 86400.0


# ---- helpers (mirror test_gc.py's, kept local so this file is self-contained) ---


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


def _scratchpad_row(store, *, tree_root_id: str = "p1", repo_root: str = "/repo") -> None:
    store.conn.execute(
        tree_kv.insert().values(
            tree_root_id=tree_root_id,
            repo_root=repo_root,
            namespace="ns",
            key="key",
            value="value",
            updated_at=now(),
            updated_by=tree_root_id,
        )
    )


def _count(store, table) -> int:
    return store.conn.execute(select(func.count()).select_from(table)).scalar()


# ---- 1. RPC returns every documented key with matching counts ----------------


async def test_gc_rpc_returns_all_keys_with_matching_counts(client, daemon, fake_tmux):
    """The response must carry every documented key, and the counts must match
    what actually disappeared from the tables."""
    _participant(daemon.store, pid="p1")
    _job(
        daemon.store,
        handle="old1",
        target_id="p1",
        state=JobState.DONE,
        finished_at=now() - 90 * _DAY,
        created_at=now() - 90 * _DAY,
    )
    _touch(daemon.store, job_handle="old1", path="x.py")
    _bus(daemon.store, kind="job.created", ts=now() - 30 * _DAY)
    _scratchpad_row(daemon.store)

    before_jobs = _count(daemon.store, jobs)
    before_touch = _count(daemon.store, touch)
    before_bus = _count(daemon.store, bus)
    before_part = _count(daemon.store, participants)
    before_scratchpad = _count(daemon.store, tree_kv)

    data = await client.call("gc")

    expected_keys = {
        "bus",
        "jobs",
        "touch",
        "participants",
        "running_marked",
        "scratchpad",
        "coverage",
        "db_bytes_before",
        "db_bytes_after",
        "vacuum_ran",
    }
    assert set(data) == expected_keys

    assert data["jobs"] == before_jobs - _count(daemon.store, jobs)
    assert data["touch"] == before_touch - _count(daemon.store, touch)
    assert data["bus"] == before_bus - _count(daemon.store, bus)
    assert data["participants"] == before_part - _count(daemon.store, participants)
    assert data["scratchpad"] == before_scratchpad - _count(daemon.store, tree_kv)

    assert isinstance(data["coverage"], dict)
    assert set(data["coverage"]) == {"jobs_from", "bus_from"}

    assert data["db_bytes_before"] >= 0
    assert data["db_bytes_after"] >= 0
    assert data["vacuum_ran"] is False


# ---- 2. live_handles protects a live running job -----------------------------


async def test_gc_rpc_passes_live_handles(client, daemon, fake_tmux):
    """A stale running job whose handle is live (held by the daemon's
    JobManager) must not be marked crashed by a manual ``theater gc``.

    Asserted against the database, not by inspecting arguments: the job row
    must still be ``running`` after the sweep.
    """
    _participant(daemon.store, pid="p1")
    _job(
        daemon.store,
        handle="live-job",
        target_id="p1",
        state=JobState.RUNNING,
        finished_at=None,
        created_at=now() - 30 * _DAY,
    )

    # Simulate the JobManager holding an asyncio.Event for this handle.
    daemon.jobs._events["live-job"] = asyncio.Event()

    await client.call("gc")

    job = daemon.store.get_job("live-job")
    assert job is not None
    assert job.state == "running"
    assert job.finished_at is None


# ---- 3. vacuum=False does not vacuum; vacuum=True does, after sweep -----------


async def test_gc_rpc_vacuum_false_does_not_vacuum(client, daemon, fake_tmux, monkeypatch):
    called = []
    import theater.daemon.gc as gc_mod

    monkeypatch.setattr(gc_mod, "vacuum", lambda store: called.append("vacuum"))
    await client.call("gc", vacuum=False)
    assert called == []


async def test_gc_rpc_vacuum_true_runs_vacuum(client, daemon, fake_tmux, monkeypatch):
    called = []
    import theater.daemon.gc as gc_mod

    monkeypatch.setattr(gc_mod, "vacuum", lambda store: called.append("vacuum"))
    await client.call("gc", vacuum=True)
    assert called == ["vacuum"]


async def test_gc_rpc_sweep_runs_before_vacuum(client, daemon, fake_tmux, monkeypatch):
    """Vacuum must run after the sweep: vacuuming before would rewrite the
    file including rows about to be deleted."""
    order = []
    import theater.daemon.gc as gc_mod

    real_sweep = gc_mod.sweep

    async def spy_sweep(store, retention, *, live_handles=frozenset()):
        order.append("sweep")
        return await real_sweep(store, retention, live_handles=live_handles)

    monkeypatch.setattr(gc_mod, "sweep", spy_sweep)
    monkeypatch.setattr(gc_mod, "vacuum", lambda store: order.append("vacuum"))

    await client.call("gc", vacuum=True)
    assert order == ["sweep", "vacuum"]


# ---- 4. RPC still sweeps when retention.enabled is false ---------------------


async def test_gc_rpc_sweeps_when_retention_disabled(client, daemon, fake_tmux):
    """``retention.enabled`` governs the automatic loop, not an explicit
    user command. The RPC must sweep regardless."""
    from theater.config import Config, RetentionSection

    daemon.config = Config(retention=RetentionSection(enabled=False))

    _participant(daemon.store, pid="p1")
    _job(
        daemon.store,
        handle="old1",
        target_id="p1",
        state=JobState.DONE,
        finished_at=now() - 90 * _DAY,
        created_at=now() - 90 * _DAY,
    )

    data = await client.call("gc")
    assert data["jobs"] == 1


# ---- 5. Rendering: nothing deleted says so, no wall of zeroes -----------------


def _render(monkeypatch, capsys, payload: dict, *argv: str) -> str:
    monkeypatch.setattr(cli, "call_sync", lambda method, **kw: payload)
    assert cli.cmd_gc(cli._parser().parse_args(["gc", *argv])) == 0
    return capsys.readouterr().out


def _gc_payload(**over) -> dict:
    base = {
        "bus": 0,
        "jobs": 0,
        "touch": 0,
        "participants": 0,
        "running_marked": 0,
        "scratchpad": 0,
        "coverage": {"jobs_from": None, "bus_from": None},
        "db_bytes_before": 1024,
        "db_bytes_after": 1024,
        "vacuum_ran": False,
    }
    return {**base, **over}


def test_render_nothing_deleted_says_so(monkeypatch, capsys):
    out = _render(monkeypatch, capsys, _gc_payload())
    assert "nothing to collect" in out
    assert "0 bus" not in out


def test_render_nothing_deleted_shows_coverage(monkeypatch, capsys):
    out = _render(monkeypatch, capsys, _gc_payload())
    assert "coverage:" in out
    assert "no data" in out


# ---- 6. Rendering: rows deleted without --vacuum tells the user ---------------


def test_render_deleted_without_vacuum_warns_file_did_not_shrink(monkeypatch, capsys):
    """The single most important string in the command: without it, a user
    who deleted 94% of the database and saw the file not shrink will report
    GC as broken."""
    out = _render(
        monkeypatch,
        capsys,
        _gc_payload(
            bus=500,
            jobs=10,
            touch=10,
            participants=2,
            running_marked=1,
            scratchpad=3,
        ),
    )
    assert "file size unchanged" in out
    assert "--vacuum" in out
    assert "3 scratchpad" in out


def test_render_scratchpad_counts_as_collection(monkeypatch, capsys):
    out = _render(monkeypatch, capsys, _gc_payload(scratchpad=1))
    assert "nothing to collect" not in out
    assert "1 scratchpad" in out


def test_render_deleted_with_vacuum_reports_reclaimed_space(monkeypatch, capsys):
    out = _render(
        monkeypatch,
        capsys,
        _gc_payload(
            bus=500,
            db_bytes_before=1_048_576,
            db_bytes_after=524_288,
            vacuum_ran=True,
        ),
    )
    assert "vacuum reclaimed" in out
    assert "file size unchanged" not in out


def test_render_vacuum_with_nothing_reclaimed_says_so(monkeypatch, capsys):
    out = _render(
        monkeypatch,
        capsys,
        _gc_payload(
            db_bytes_before=1024,
            db_bytes_after=1024,
            vacuum_ran=True,
        ),
    )
    assert "vacuum ran" in out
    assert "unchanged" in out


# ---- 7. --json emits the raw response ----------------------------------------


def test_json_emits_raw_response(monkeypatch, capsys):
    payload = _gc_payload(bus=3, jobs=1)
    monkeypatch.setattr(cli, "call_sync", lambda method, **kw: payload)
    assert cli.cmd_gc(cli._parser().parse_args(["gc", "--json"])) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == payload
    # None of the prose should appear in --json output.
    assert "nothing to collect" not in out
    assert "file size unchanged" not in out
    assert "coverage:" not in out
