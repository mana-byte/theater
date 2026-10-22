"""`theater gc`: the RPC's counts and protections, and how the sweep is reported."""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import func, select

from tests.support.builders import make_bus_row, make_job, make_participant, make_touch
from theater import cli
from theater.cli.commands import maintenance as maintenance_mod
from theater.daemon.jobs import JobState
from theater.daemon.schema import bus, global_scratchpad, jobs, participants, touch
from theater.models import now

_DAY = 86400.0


def _scratchpad_row(store) -> None:
    store.conn.execute(
        global_scratchpad.insert().values(
            namespace="ns",
            key="key",
            value="value",
            updated_at=now(),
            expires_at=now() - 1,
        )
    )


def _count(store, table) -> int:
    return store.conn.execute(select(func.count()).select_from(table)).scalar()


# ---- RPC returns every documented key with matching counts


async def test_gc_rpc_returns_all_keys_with_matching_counts(client, daemon, terminal_provider):
    """The response must carry every documented key, and the counts must match
    what actually disappeared from the tables."""
    make_participant(daemon.store, pid="p1")
    make_job(
        daemon.store,
        handle="old1",
        target_id="p1",
        state=JobState.DONE,
        finished_at=now() - 90 * _DAY,
        created_at=now() - 90 * _DAY,
    )
    make_touch(daemon.store, job_handle="old1", path="x.py")
    make_bus_row(daemon.store, kind="job.created", ts=now() - 30 * _DAY)
    _scratchpad_row(daemon.store)

    before_jobs = _count(daemon.store, jobs)
    before_touch = _count(daemon.store, touch)
    before_bus = _count(daemon.store, bus)
    before_part = _count(daemon.store, participants)
    before_scratchpad = _count(daemon.store, global_scratchpad)

    data = await client.call("gc")

    expected_keys = {
        "bus",
        "jobs",
        "touch",
        "participants",
        "running_marked",
        "scratchpad",
        "control_operations",
        "native_evidence",
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
    assert data["scratchpad"] == before_scratchpad - _count(daemon.store, global_scratchpad)

    assert isinstance(data["coverage"], dict)
    assert set(data["coverage"]) == {"jobs_from", "bus_from"}

    assert data["db_bytes_before"] >= 0
    assert data["db_bytes_after"] >= 0
    assert data["vacuum_ran"] is False


# ---- live_handles protects a live running job


async def test_gc_rpc_passes_live_handles(client, daemon, terminal_provider):
    """A stale running job whose handle is live (held by the daemon's
    JobManager) must not be marked crashed by a manual ``theater gc``.

    Asserted against the database, not by inspecting arguments: the job row
    must still be ``running`` after the sweep.
    """
    make_participant(daemon.store, pid="p1")
    make_job(
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


async def test_manual_gc_does_not_wake_handoffs_after_marking_a_running_job(
    client, daemon, terminal_provider, monkeypatch
):
    make_participant(daemon.store, pid="old-target")
    make_job(
        daemon.store,
        handle="stale-source",
        target_id="old-target",
        state=JobState.RUNNING,
        finished_at=None,
        created_at=now() - 30 * _DAY,
    )
    wakes = []
    monkeypatch.setattr(daemon.handoff, "wake", lambda: wakes.append("wake"))

    data = await client.call("gc")

    assert data["running_marked"] == 1
    assert daemon.store.get_job("stale-source").state == JobState.CRASHED
    assert wakes == []


# ---- vacuum=False does not vacuum; vacuum=True does, after sweep


async def test_gc_rpc_vacuum_false_does_not_vacuum(client, daemon, terminal_provider, monkeypatch):
    called = []
    import theater.daemon.gc as gc_mod

    monkeypatch.setattr(gc_mod, "vacuum", lambda store: called.append("vacuum"))
    await client.call("gc", vacuum=False)
    assert called == []


async def test_gc_rpc_vacuum_true_runs_vacuum(client, daemon, terminal_provider, monkeypatch):
    called = []
    import theater.daemon.gc as gc_mod

    monkeypatch.setattr(gc_mod, "vacuum", lambda store: called.append("vacuum"))
    await client.call("gc", vacuum=True)
    assert called == ["vacuum"]


async def test_gc_rpc_sweep_runs_before_vacuum(client, daemon, terminal_provider, monkeypatch):
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


# ---- RPC still sweeps when retention.enabled is false


async def test_gc_rpc_sweeps_when_retention_disabled(client, daemon, terminal_provider):
    """``retention.enabled`` governs the automatic loop, not an explicit
    user command. The RPC must sweep regardless."""
    from theater.config import Config, RetentionSection

    daemon.config = Config(retention=RetentionSection(enabled=False))

    make_participant(daemon.store, pid="p1")
    make_job(
        daemon.store,
        handle="old1",
        target_id="p1",
        state=JobState.DONE,
        finished_at=now() - 90 * _DAY,
        created_at=now() - 90 * _DAY,
    )

    data = await client.call("gc")
    assert data["jobs"] == 1


# ---- Rendering: nothing deleted says so, no wall of zeroes


def _render(monkeypatch, capsys, payload: dict, *argv: str) -> str:
    monkeypatch.setattr(maintenance_mod, "call_sync", lambda method, **kw: payload)
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


# ---- Rendering: rows deleted without --vacuum tells the user


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


# ---- --json emits the raw response


def test_json_emits_raw_response(monkeypatch, capsys):
    payload = _gc_payload(bus=3, jobs=1)
    monkeypatch.setattr(maintenance_mod, "call_sync", lambda method, **kw: payload)
    assert cli.cmd_gc(cli._parser().parse_args(["gc", "--json"])) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == payload
    # None of the prose should appear in --json output.
    assert "nothing to collect" not in out
    assert "file size unchanged" not in out
    assert "coverage:" not in out
