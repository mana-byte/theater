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


# ---- checkpoint.list ------------------------------------------------------


async def test_checkpoint_list_returns_caller_checkpoints_newest_first(client, daemon):
    caller = await client.call("hello", id="caller", harness="vibe", cwd="/tmp")
    first = await client.call(
        "checkpoint.create", caller_id=caller["id"], name="first", notes="alpha"
    )
    second = await client.call(
        "checkpoint.create", caller_id=caller["id"], name="second", notes="beta"
    )

    rows = await client.call("checkpoint.list", caller_id=caller["id"])

    assert [r["id"] for r in rows] == [second["checkpoint_id"], first["checkpoint_id"]]
    assert rows[0]["name"] == "second"
    assert rows[1]["name"] == "first"


async def test_checkpoint_list_empty_when_none(client):
    caller = await client.call("hello", id="caller", harness="vibe", cwd="/tmp")
    rows = await client.call("checkpoint.list", caller_id=caller["id"])
    assert rows == []


async def test_checkpoint_list_returns_all_participants_checkpoints(client, daemon):
    a = await client.call("hello", id="a", harness="vibe", cwd="/tmp")
    b = await client.call("hello", id="b", harness="vibe", cwd="/tmp")
    await client.call("checkpoint.create", caller_id=a["id"], name="a-checkpoint")
    await client.call("checkpoint.create", caller_id=b["id"], name="b-checkpoint")

    # Any caller can see all checkpoints.
    rows_from_a = await client.call("checkpoint.list", caller_id=a["id"])
    names = {r["name"] for r in rows_from_a}
    assert names == {"a-checkpoint", "b-checkpoint"}

    # participant_id filter still narrows correctly.
    rows_filtered = await client.call("checkpoint.list", caller_id=a["id"], participant_id=a["id"])
    assert [r["name"] for r in rows_filtered] == ["a-checkpoint"]


async def test_checkpoint_list_response_shape(client):
    caller = await client.call("hello", id="caller", harness="vibe", cwd="/tmp")
    created = await client.call(
        "checkpoint.create", caller_id=caller["id"], name="cp", notes="short"
    )

    rows = await client.call("checkpoint.list", caller_id=caller["id"])
    assert set(rows[0]) == {
        "id",
        "participant_id",
        "creator_name",
        "creator_status",
        "creator_present",
        "name",
        "created_at",
        "restore_state",
        "restored_by",
        "restored_at",
        "notes",
        "notes_truncated",
    }
    assert rows[0]["id"] == created["checkpoint_id"]
    assert rows[0]["notes"] == "short"
    assert rows[0]["notes_truncated"] is False
    assert rows[0]["participant_id"] == caller["id"]
    assert rows[0]["creator_present"] is True
    assert rows[0]["restore_state"] == "ready"


async def test_checkpoint_list_truncates_long_notes(client):
    caller = await client.call("hello", id="caller", harness="vibe", cwd="/tmp")
    long_notes = "x" * 500
    await client.call("checkpoint.create", caller_id=caller["id"], name="cp", notes=long_notes)

    rows = await client.call("checkpoint.list", caller_id=caller["id"])
    assert len(rows[0]["notes"]) == 300
    assert rows[0]["notes_truncated"] is True


async def test_checkpoint_list_notes_none(client):
    caller = await client.call("hello", id="caller", harness="vibe", cwd="/tmp")
    await client.call("checkpoint.create", caller_id=caller["id"], name="cp", notes=None)

    rows = await client.call("checkpoint.list", caller_id=caller["id"])
    assert rows[0]["notes"] is None
    assert rows[0]["notes_truncated"] is False


async def test_checkpoint_list_limit(client, daemon):
    caller = await client.call("hello", id="caller", harness="vibe", cwd="/tmp")
    for i in range(5):
        await client.call("checkpoint.create", caller_id=caller["id"], name=f"cp-{i}")

    rows = await client.call("checkpoint.list", caller_id=caller["id"], limit=3)
    assert len(rows) == 3


async def test_checkpoint_list_rejects_invalid_limit(client):
    caller = await client.call("hello", id="caller", harness="vibe", cwd="/tmp")
    for raw in [0, -1, 101, True, "ten"]:
        with pytest.raises(RemoteError) as exc:
            await client.call("checkpoint.list", caller_id=caller["id"], limit=raw)
        assert exc.value.code == "bad_request"


async def test_checkpoint_list_requires_existing_caller(client):
    with pytest.raises(RemoteError) as exc:
        await client.call("checkpoint.list", caller_id="ghost")
    assert exc.value.code == "bad_request"
    assert "existing participant" in str(exc.value)


async def test_checkpoint_list_rejects_empty_participant_id(client):
    caller = await client.call("hello", id="caller", harness="vibe", cwd="/tmp")
    with pytest.raises(RemoteError) as exc:
        await client.call("checkpoint.list", caller_id=caller["id"], participant_id="")
    assert exc.value.code == "bad_request"
    assert "empty" in str(exc.value)


async def test_checkpoint_list_participant_b_sees_participant_a_checkpoint(client, daemon):
    a = await client.call("hello", id="a", harness="vibe", cwd="/tmp")
    b = await client.call("hello", id="b", harness="vibe", cwd="/tmp")
    created = await client.call("checkpoint.create", caller_id=a["id"], name="a-cp")

    rows = await client.call("checkpoint.list", caller_id=b["id"])
    assert len(rows) == 1
    assert rows[0]["id"] == created["checkpoint_id"]
    assert rows[0]["participant_id"] == a["id"]


async def test_checkpoint_list_restorable_only_respects_limit_not_filter_then_limit(client, daemon):
    """restorable_only must filter inside SQL (before LIMIT), not after.

    If the newest `limit` rows are all non-ready, limit-then-filter returns [].
    This test creates `limit` restored checkpoints (newest) then one ready
    checkpoint (oldest), and asserts that restorable_only=True with that same
    limit still returns the ready one.
    """
    from theater.models import Participant, Tier

    parent = Participant(
        id="parent-limit", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%91"
    )
    daemon.store.upsert_participant(parent)
    restorer = Participant(
        id="restorer-limit", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%92"
    )
    daemon.store.upsert_participant(restorer)
    await client.call("hello", id="restorer-limit", harness="vibe", cwd="/tmp")

    # Create 3 checkpoints that will be restored (they will be made newer via
    # created_at manipulation, so they sort first).
    from sqlalchemy import update as sa_update

    from theater.daemon.schema import checkpoints as ckpt_table

    # Use a very large base timestamp so these are definitively newest.
    far_future = 9_999_999_999.0
    for i in range(3):
        cp = await client.call(
            "checkpoint.create", caller_id="parent-limit", name=f"to-restore-{i}"
        )
        daemon.store.conn.execute(
            sa_update(ckpt_table)
            .where(ckpt_table.c.id == cp["checkpoint_id"])
            .values(created_at=far_future + i)
        )
        await client.call(
            "checkpoint.restore",
            checkpoint_id=cp["checkpoint_id"],
            approval="yolo",
            caller_id="restorer-limit",
        )

    # Create the ready checkpoint AFTER the restores, but pin its created_at
    # to a very small value so it sorts last (oldest).
    ready_cp = await client.call("checkpoint.create", caller_id="parent-limit", name="ready-one")
    daemon.store.conn.execute(
        sa_update(ckpt_table)
        .where(ckpt_table.c.id == ready_cp["checkpoint_id"])
        .values(created_at=1.0)
    )

    # With limit=3, the 3 newest rows are all restored; the ready one is row 4.
    # restorable_only=True with limit=3 must still return the ready one —
    # SQL WHERE must be applied before LIMIT, not after.
    restorable = await client.call(
        "checkpoint.list",
        caller_id="restorer-limit",
        participant_id="parent-limit",
        limit=3,
        restorable_only=True,
    )
    assert len(restorable) == 1, (
        "restorable_only with limit must filter in SQL; got empty list because "
        "the limit was applied before the filter"
    )
    assert restorable[0]["id"] == ready_cp["checkpoint_id"]


async def test_checkpoint_list_restorable_only_excludes_non_ready(client, daemon):
    from theater.models import Participant, Tier

    parent = Participant(id="parent", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%1")
    daemon.store.upsert_participant(parent)
    caller = Participant(id="caller", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%2")
    daemon.store.upsert_participant(caller)
    await client.call("hello", id="caller", harness="vibe", cwd="/tmp")

    ready_cp = await client.call("checkpoint.create", caller_id="parent", name="ready")
    restored_cp = await client.call("checkpoint.create", caller_id="parent", name="restored")

    # restore the second checkpoint so it transitions to 'restored'
    await client.call(
        "checkpoint.restore",
        checkpoint_id=restored_cp["checkpoint_id"],
        approval="yolo",
        caller_id="caller",
    )

    all_rows = await client.call("checkpoint.list", caller_id="caller")
    assert len(all_rows) == 2

    restorable = await client.call("checkpoint.list", caller_id="caller", restorable_only=True)
    assert len(restorable) == 1
    assert restorable[0]["id"] == ready_cp["checkpoint_id"]


async def test_checkpoint_list_creator_pruned_shows_creator_present_false(client, daemon):
    parent = await client.call("hello", id="parent", harness="vibe", cwd="/tmp")
    created = await client.call("checkpoint.create", caller_id=parent["id"], name="cp")

    # Record the name before pruning — it should survive as creator_name.
    rows_before = await client.call("checkpoint.list", caller_id=parent["id"])
    name_before = rows_before[0]["creator_name"]

    # Prune the creator participant from the store.
    import sqlalchemy

    from theater.daemon.schema import participants as part_table

    daemon.store.conn.execute(sqlalchemy.delete(part_table).where(part_table.c.id == parent["id"]))

    caller = await client.call("hello", id="caller2", harness="vibe", cwd="/tmp")
    rows = await client.call("checkpoint.list", caller_id=caller["id"])
    assert len(rows) == 1
    assert rows[0]["id"] == created["checkpoint_id"]
    assert rows[0]["creator_present"] is False
    # creator_name is snapshotted at creation time and survives the creator's death.
    assert rows[0]["creator_name"] == name_before


async def test_checkpoint_read_on_another_participants_checkpoint(client, daemon):
    creator = await client.call("hello", id="creator", harness="vibe", cwd="/tmp")
    created = await client.call("checkpoint.create", caller_id=creator["id"], name="cp")

    reader = await client.call("hello", id="reader", harness="vibe", cwd="/tmp")
    read = await client.call(
        "checkpoint.read", checkpoint_id=created["checkpoint_id"], caller_id=reader["id"]
    )
    assert read["checkpoint"]["id"] == created["checkpoint_id"]
    assert read["checkpoint"]["participant_id"] == creator["id"]


async def test_checkpoint_creator_cannot_self_restore_after_global_list(client, daemon):
    caller = await client.call("hello", id="caller", harness="vibe", cwd="/tmp")
    created = await client.call("checkpoint.create", caller_id=caller["id"], name="cp")
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id=caller["id"],
        )
    assert exc.value.code == "bad_request"
    assert "self-restore" in str(exc.value)


async def test_checkpoint_full_loop_a_creates_a_dies_b_lists_b_restores(client, daemon):
    from theater.models import Participant, Tier

    a = Participant(id="a", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%1")
    daemon.store.upsert_participant(a)
    created = await client.call("checkpoint.create", caller_id="a", name="handoff")

    b = Participant(id="b", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%2")
    daemon.store.upsert_participant(b)
    await client.call("hello", id="b", harness="vibe", cwd="/tmp")

    # B lists and finds A's checkpoint.
    rows = await client.call("checkpoint.list", caller_id="b")
    assert any(r["id"] == created["checkpoint_id"] for r in rows)

    # B reads the checkpoint.
    read = await client.call("checkpoint.read", checkpoint_id=created["checkpoint_id"])
    assert read["checkpoint"]["participant_id"] == "a"

    # B restores A's checkpoint (A is live so action='live').
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="b",
    )
    assert result["restored_parent"]["action"] == "live"

    # restored_by recorded.
    read2 = await client.call("checkpoint.read", checkpoint_id=created["checkpoint_id"])
    assert read2["checkpoint"]["restored_by"] == "b"
    assert read2["checkpoint"]["restore_state"] == "restored"


async def test_checkpoint_concurrent_restore_names_the_holder(client, daemon):
    from theater.models import Participant, Status, Tier

    parent = Participant(
        id="parent-cc", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%10"
    )
    daemon.store.upsert_participant(parent)
    daemon.store.set_status(parent.id, Status.DEAD)
    created = await client.call("checkpoint.create", caller_id="parent-cc", name="cp")

    b = Participant(id="b-cc", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%11")
    daemon.store.upsert_participant(b)
    c = Participant(id="c-cc", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%12")
    daemon.store.upsert_participant(c)
    await client.call("hello", id="b-cc", harness="vibe", cwd="/tmp")
    await client.call("hello", id="c-cc", harness="vibe", cwd="/tmp")

    # Manually claim the restore as B so it's in 'restoring' state.
    token = daemon.store.claim_checkpoint_restore(created["checkpoint_id"], "b-cc")
    assert token is not None

    # C tries to restore and should get checkpoint_restore_in_progress naming B.
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id="c-cc",
        )
    assert exc.value.code == "checkpoint_restore_in_progress"
    assert "b-cc" in str(exc.value)


# ---- checkpoint.restore ---------------------------------------------------


async def test_checkpoint_restore_rejects_invalid_approval(client, daemon):
    caller = await client.call("hello", id="caller", harness="vibe", cwd="/tmp")
    created = await client.call("checkpoint.create", caller_id=caller["id"], name="cp")
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="invalid",
            caller_id=caller["id"],
        )
    assert exc.value.code == "bad_request"


async def test_checkpoint_restore_rejects_self_restore(client, daemon):
    caller = await client.call("hello", id="caller", harness="vibe", cwd="/tmp")
    created = await client.call("checkpoint.create", caller_id=caller["id"], name="cp")
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id=caller["id"],
        )
    assert exc.value.code == "bad_request"
    assert "self-restore" in str(exc.value)


async def test_checkpoint_restore_rejects_missing_checkpoint(client):
    caller = await client.call("hello", id="caller", harness="vibe", cwd="/tmp")
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore", checkpoint_id=999, approval="yolo", caller_id=caller["id"]
        )
    assert exc.value.code == "bad_request"
    assert "no checkpoint" in str(exc.value)


async def test_checkpoint_restore_rejects_external_parent(client, daemon):
    external = await client.call("hello", id="ext", harness="vibe")
    created = await client.call("checkpoint.create", caller_id=external["id"], name="cp")
    caller = await client.call("hello", id="caller", harness="vibe", cwd="/tmp")
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id=caller["id"],
        )
    assert exc.value.code == "bad_request"
    assert "EXTERNAL" in str(exc.value)


async def test_checkpoint_restore_rejects_pruned_parent(client, daemon):
    parent = await client.call("hello", id="parent", harness="vibe", cwd="/tmp")
    created = await client.call("checkpoint.create", caller_id=parent["id"], name="cp")
    daemon.store.conn.execute(
        __import__("sqlalchemy")
        .delete(__import__("theater.daemon.schema", fromlist=["participants"]).participants)
        .where(
            __import__("theater.daemon.schema", fromlist=["participants"]).participants.c.id
            == parent["id"]
        )
    )
    caller = await client.call("hello", id="caller2", harness="vibe", cwd="/tmp")  # noqa: F841
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id="caller2",
        )
    assert exc.value.code == "bad_request"
    assert "pruned" in str(exc.value)


async def test_checkpoint_restore_exposes_state_in_read(client, daemon):
    caller = await client.call("hello", id="caller", harness="vibe", cwd="/tmp")
    created = await client.call("checkpoint.create", caller_id=caller["id"], name="cp")
    read = await client.call("checkpoint.read", checkpoint_id=created["checkpoint_id"])
    assert read["checkpoint"]["restore_state"] == "ready"
    assert read["checkpoint"]["restored_at"] is None
    assert read["checkpoint"]["restore_error"] is None


async def test_checkpoint_restore_state_machine_claim_and_finalize(store):
    cid = store.create_checkpoint(participant_id="p1", name="cp", jobs_snapshot="[]")
    token = store.claim_checkpoint_restore(cid, "caller1")
    assert token is not None

    row = store.get_checkpoint(cid)
    assert row["restore_state"] == "restoring"
    assert row["restore_token"] == token

    ok = store.finalize_checkpoint_restore(
        cid, token=token, restored_by="caller1", result='{"action":"live"}'
    )
    assert ok is True
    row = store.get_checkpoint(cid)
    assert row["restore_state"] == "restored"
    assert row["restored_at"] is not None
    assert row["restore_result"] == '{"action":"live"}'


async def test_checkpoint_restore_concurrent_claim_fails(store):
    cid = store.create_checkpoint(participant_id="p1", name="cp", jobs_snapshot="[]")
    token1 = store.claim_checkpoint_restore(cid, "caller1")
    assert token1 is not None
    token2 = store.claim_checkpoint_restore(cid, "caller2")
    assert token2 is None


async def test_checkpoint_restore_wrong_token_cannot_finalize(store):
    cid = store.create_checkpoint(participant_id="p1", name="cp", jobs_snapshot="[]")
    token = store.claim_checkpoint_restore(cid, "caller1")
    assert token is not None
    ok = store.finalize_checkpoint_restore(cid, token="wrong-token", restored_by="caller1")
    assert ok is False
    row = store.get_checkpoint(cid)
    assert row["restore_state"] == "restoring"


async def test_checkpoint_restore_failure_sets_failed_state(store):
    cid = store.create_checkpoint(participant_id="p1", name="cp", jobs_snapshot="[]")
    token = store.claim_checkpoint_restore(cid, "caller1")
    assert token is not None
    ok = store.finalize_checkpoint_restore(
        cid, token=token, restored_by="caller1", error="spawn failed"
    )
    assert ok is True
    row = store.get_checkpoint(cid)
    assert row["restore_state"] == "failed"
    assert row["restore_error"] == "spawn failed"


async def test_checkpoint_restore_recover_stranded(store):
    cid = store.create_checkpoint(participant_id="p1", name="cp", jobs_snapshot="[]")
    token = store.claim_checkpoint_restore(cid, "caller1")
    assert token is not None

    stranded = store.recover_stranded_restores()
    assert stranded == 1
    row = store.get_checkpoint(cid)
    assert row["restore_state"] == "failed"
    assert "daemon restarted" in row["restore_error"]


async def test_checkpoint_restore_release_returns_to_ready(store):
    cid = store.create_checkpoint(participant_id="p1", name="cp", jobs_snapshot="[]")
    token = store.claim_checkpoint_restore(cid, "caller1")
    assert token is not None

    ok = store.release_checkpoint_restore(cid, token=token)
    assert ok is True
    row = store.get_checkpoint(cid)
    assert row["restore_state"] == "ready"
    assert row["restore_token"] is None

    token2 = store.claim_checkpoint_restore(cid, "caller2")
    assert token2 is not None


async def test_checkpoint_restore_wrong_token_cannot_release(store):
    cid = store.create_checkpoint(participant_id="p1", name="cp", jobs_snapshot="[]")
    token = store.claim_checkpoint_restore(cid, "caller1")
    assert token is not None
    ok = store.release_checkpoint_restore(cid, token="wrong-token")
    assert ok is False
    row = store.get_checkpoint(cid)
    assert row["restore_state"] == "restoring"


async def test_checkpoint_restore_live_parent_rejected_when_ancestor(client, daemon):
    from sqlalchemy import update as sa_update

    from theater.daemon.schema import participants as part_table
    from theater.models import Participant, Tier

    ancestor = Participant(
        id="ancestor", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%1"
    )
    descendant = Participant(
        id="descendant", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%2"
    )
    daemon.store.upsert_participant(ancestor)
    daemon.store.upsert_participant(descendant)
    daemon.store.conn.execute(
        sa_update(part_table).where(part_table.c.id == descendant.id).values(parent_id=ancestor.id)
    )
    created = await client.call("checkpoint.create", caller_id="ancestor", name="cp")
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id="descendant",
        )
    assert exc.value.code == "bad_request"
    assert "awaiting its response would close a cycle" in str(exc.value)


async def test_checkpoint_restore_live_parent_succeeds(client, daemon):
    from theater.models import Participant, Tier

    parent = Participant(id="parent", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%1")
    daemon.store.upsert_participant(parent)
    created = await client.call("checkpoint.create", caller_id="parent", name="cp")
    caller = Participant(id="caller", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%2")
    daemon.store.upsert_participant(caller)
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="caller",
    )
    assert result["restored_parent"]["action"] == "live"
    assert result["restored_parent"]["participant_id"] == "parent"
    assert result["restored_parent"]["handoff_required"] is True


async def test_checkpoint_restore_live_parent_without_pane_rejected(client, daemon):
    from theater.models import Participant, Tier

    parent = Participant(id="parent", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane=None)
    daemon.store.upsert_participant(parent)
    created = await client.call("checkpoint.create", caller_id="parent", name="cp")
    caller = Participant(id="caller", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%2")
    daemon.store.upsert_participant(caller)
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id="caller",
        )
    assert exc.value.code == "bad_request"
    assert "no pane" in str(exc.value)


async def test_checkpoint_restore_result_durable_in_read(client, daemon):
    from theater.models import Participant, Tier

    parent = Participant(id="parent", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%1")
    daemon.store.upsert_participant(parent)
    created = await client.call("checkpoint.create", caller_id="parent", name="cp")
    caller = Participant(id="caller", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%2")
    daemon.store.upsert_participant(caller)
    await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="caller",
    )
    read = await client.call("checkpoint.read", checkpoint_id=created["checkpoint_id"])
    assert read["checkpoint"]["restore_state"] == "restored"
    assert read["checkpoint"]["restored_at"] is not None
    assert read["checkpoint"]["restored_by"] == "caller"
    result = read["checkpoint"]["restore_result"]
    assert result is not None
    assert result["action"] == "live"
    assert result["handoff_required"] is True


async def test_checkpoint_restore_second_attempt_refused(client, daemon):
    from theater.models import Participant, Tier

    parent = Participant(id="parent", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%1")
    daemon.store.upsert_participant(parent)
    created = await client.call("checkpoint.create", caller_id="parent", name="cp")
    caller = Participant(id="caller", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%2")
    daemon.store.upsert_participant(caller)
    await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="caller",
    )
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id="caller",
        )
    assert exc.value.code == "checkpoint_already_restored"


async def test_checkpoint_restore_spawn_failure_marks_failed(client, daemon, monkeypatch):
    import theater.daemon.methods as methods_mod
    from theater.models import Participant, Status, Tier

    parent = Participant(id="parent", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%1")
    daemon.store.upsert_participant(parent)
    daemon.store.set_status(parent.id, Status.DEAD)
    created = await client.call("checkpoint.create", caller_id="parent", name="cp")
    caller = Participant(id="caller", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%2")
    daemon.store.upsert_participant(caller)

    async def _fake_spawn(daemon, params):
        raise RuntimeError("tmux exploded")

    monkeypatch.setattr(methods_mod, "_spawn", _fake_spawn)

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id="caller",
        )
    assert "tmux exploded" in str(exc.value)

    read = await client.call("checkpoint.read", checkpoint_id=created["checkpoint_id"])
    assert read["checkpoint"]["restore_state"] == "failed"
    assert "tmux exploded" in read["checkpoint"]["restore_error"]


async def test_checkpoint_restore_spawn_cancelled_marks_failed(client, daemon, monkeypatch):
    import asyncio as _asyncio

    import theater.daemon.methods as methods_mod
    from theater.models import Participant, Status, Tier

    parent = Participant(id="parent", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%1")
    daemon.store.upsert_participant(parent)
    daemon.store.set_status(parent.id, Status.DEAD)
    created = await client.call("checkpoint.create", caller_id="parent", name="cp")
    caller = Participant(id="caller", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%2")
    daemon.store.upsert_participant(caller)

    async def _fake_spawn(daemon, params):
        raise _asyncio.CancelledError()

    monkeypatch.setattr(methods_mod, "_spawn", _fake_spawn)

    # CancelledError propagates through the daemon and drops the connection.
    with pytest.raises((ConnectionError, _asyncio.CancelledError)):
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id="caller",
        )

    # Reconnect and verify the checkpoint was marked failed.
    await client._drop()
    await client.connect()
    read = await client.call("checkpoint.read", checkpoint_id=created["checkpoint_id"])
    assert read["checkpoint"]["restore_state"] == "failed"
    assert "cancelled" in read["checkpoint"]["restore_error"]
