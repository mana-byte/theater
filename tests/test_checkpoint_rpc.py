"""Daemon RPC tests for cumulative checkpoints."""

from __future__ import annotations

import pytest

from theater.daemon.schema import jobs as jobs_table
from theater.models import Job, JobState
from theater.protocol import RemoteError


def _job(
    store,
    *,
    handle: str,
    caller_id: str,
    created_at: float,
    target_id: str = "target",
    kind: str = "send",
    prompt: str = "do it",
    state: str = JobState.RUNNING,
    result: str | None = None,
    error_code: str | None = None,
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
            created_at=created_at,
            finished_at=finished_at,
        )
    )


def _handles(rows: list[dict]) -> list[str]:
    return [row["handle"] for row in rows]


def _assert_snapshot_shape(row: dict) -> None:
    assert set(row) == {
        "handle",
        "target_id",
        "kind",
        "prompt",
        "state",
        "result",
        "error_code",
        "created_at",
        "finished_at",
    }


async def test_checkpoint_create_snapshots_completed_and_running_jobs(client, daemon):
    caller = await client.call("hello", id="caller", harness="vibe", cwd="/tmp")
    _job(
        daemon.store,
        handle="done-job",
        caller_id=caller["id"],
        state=JobState.DONE,
        result="done",
        created_at=1.0,
        finished_at=2.0,
    )
    _job(
        daemon.store,
        handle="running-job",
        caller_id=caller["id"],
        state=JobState.RUNNING,
        created_at=3.0,
    )

    result = await client.call(
        "checkpoint.create",
        caller_id=caller["id"],
        name="before merge",
        notes="two jobs",
    )

    assert isinstance(result["checkpoint_id"], int)
    assert _handles(result["jobs"]) == ["done-job", "running-job"]
    for row in result["jobs"]:
        _assert_snapshot_shape(row)
    assert result["jobs"][0]["state"] == "done"
    assert result["jobs"][1]["state"] == "running"


async def test_checkpoint_snapshots_are_cumulative(client, daemon):
    caller = await client.call("hello", id="caller", harness="vibe", cwd="/tmp")
    _job(daemon.store, handle="first", caller_id=caller["id"], created_at=1.0)
    first = await client.call("checkpoint.create", caller_id=caller["id"], name="one")
    _job(daemon.store, handle="second", caller_id=caller["id"], created_at=2.0)
    second = await client.call("checkpoint.create", caller_id=caller["id"], name="two")

    assert _handles(first["jobs"]) == ["first"]
    assert _handles(second["jobs"]) == ["first", "second"]


async def test_checkpoint_read_reports_recorded_live_and_pruned_jobs(client, daemon):
    caller = await client.call("hello", id="caller", harness="vibe", cwd="/tmp")
    _job(daemon.store, handle="done", caller_id=caller["id"], created_at=1.0)
    _job(daemon.store, handle="pruned", caller_id=caller["id"], created_at=2.0)
    created = await client.call(
        "checkpoint.create",
        caller_id=caller["id"],
        name="baseline",
        notes=None,
    )

    daemon.store.finish_job(
        "done",
        state=JobState.DONE,
        result="finished later",
        finished_at=5.0,
    )
    daemon.store.conn.execute(jobs_table.delete().where(jobs_table.c.handle == "pruned"))

    read = await client.call("checkpoint.read", checkpoint_id=created["checkpoint_id"])

    assert read["checkpoint"]["id"] == created["checkpoint_id"]
    assert read["checkpoint"]["participant_id"] == caller["id"]
    assert read["checkpoint"]["name"] == "baseline"
    assert read["checkpoint"]["notes"] is None
    assert "jobs_snapshot" not in read["checkpoint"]
    assert _handles(read["recorded_jobs"]) == ["done", "pruned"]
    assert _handles(read["live_jobs"]) == ["done"]
    assert read["live_jobs"][0]["state"] == "done"
    assert read["live_jobs"][0]["result"] == "finished later"
    assert read["recorded_jobs"][0]["state"] == "running"
    assert read["pruned_handles"] == ["pruned"]


async def test_checkpoint_create_requires_existing_caller(client):
    with pytest.raises(RemoteError) as exc:
        await client.call("checkpoint.create", caller_id="ghost", name="plan")

    assert exc.value.code == "bad_request"
    assert "existing participant" in str(exc.value)


@pytest.mark.parametrize("name", ["", None])
async def test_checkpoint_create_requires_non_empty_name(client, name):
    caller = await client.call("hello", id="caller", harness="vibe", cwd="/tmp")

    with pytest.raises(RemoteError) as exc:
        await client.call("checkpoint.create", caller_id=caller["id"], name=name)

    assert exc.value.code == "bad_request"
    assert "name" in str(exc.value)


async def test_checkpoint_read_rejects_malformed_id(client):
    with pytest.raises(RemoteError) as exc:
        await client.call("checkpoint.read", checkpoint_id="not-an-int")

    assert exc.value.code == "bad_request"
    assert "checkpoint_id must be an integer" in str(exc.value)


async def test_checkpoint_read_rejects_missing_checkpoint(client):
    with pytest.raises(RemoteError) as exc:
        await client.call("checkpoint.read", checkpoint_id=999)

    assert exc.value.code == "bad_request"
    assert "no checkpoint" in str(exc.value)
