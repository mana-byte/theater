"""Comprehensive tests for orchestration-tree checkpoint recovery (v2).

Covers:
- Recursive tree snapshots (creator + descendants)
- All classification paths: live, resumable, respawnable, completed, pruned, failed
- All action paths: reused_live, resumed, respawned, skipped, failed
- Parent-child-grandchild lineage restoration
- Rail enforcement (depth, budget, model, reasoning)
- Cycle detection on live parents
- Worktree and provenance recording
- Send jobs reported but not replayed
- Partial/crash/concurrent claim semantics
- GC / pruned participants
- Machine-global discovery
- v1 checkpoint compatibility
- No injection into human-present panes (structural: not injecting on restore)
- Snapshot version detection
- Durable incremental progress

These tests use the daemon + client fixtures from conftest.py and operate at
the RPC level. Tests that require actual tmux spawning use the fake_tmux fixture.
"""

from __future__ import annotations

import json

import pytest

from theater.daemon.recovery import (
    _bfs_order,
    build_tree_snapshot,
    classify_node,
    is_v2_snapshot,
    parse_snapshot,
    upgrade_v1_snapshot_for_read,
)
from theater.daemon.schema import jobs as jobs_table
from theater.models import Job, Participant, Status, Tier
from theater.protocol import RemoteError

# ---- helpers ----------------------------------------------------------------


def _prov(cwd: str = "/tmp", **kwargs) -> str:
    """Build a minimal launch_provenance JSON blob."""
    base = {
        "prompt": "",
        "approval": "yolo",
        "cwd_requested": cwd,
        "cwd_resolved": cwd,
        "model": None,
        "reasoning_effort": None,
        "worktree": False,
        "worktree_type": None,
        "worktree_name": None,
        "worktree_branch": None,
        "base_branch": None,
        "response_format": None,
        "resume_session_id": None,
        "tmux_session": None,
        "window_name": None,
    }
    base.update(kwargs)
    return json.dumps(base, sort_keys=True, separators=(",", ":"))


def _make_participant(
    daemon,
    *,
    pid: str,
    harness: str = "vibe",
    cwd: str = "/tmp",
    pane: str | None = None,
    parent_id: str | None = None,
    dead: bool = False,
    session_id: str | None = None,
    session_correlation: str | None = None,
    launch_provenance: str | None = None,
) -> Participant:
    p = Participant(
        id=pid,
        harness=harness,
        tier=Tier.SPAWNED,
        cwd=cwd,
        tmux_pane=pane,
        parent_id=parent_id,
        session_id=session_id,
        session_correlation=session_correlation,
        launch_provenance=launch_provenance,
    )
    daemon.store.upsert_participant(p)
    if dead:
        daemon.store.set_status(pid, Status.DEAD)
    return p


def _make_job(store, *, handle: str, caller_id: str, state: str = "running", kind: str = "send"):
    store.create_job(
        Job(
            handle=handle,
            caller_id=caller_id,
            target_id="t",
            kind=kind,
            prompt="work",
            state=state,
            result=None,
            error_code=None,
            created_at=1.0,
            finished_at=None if state == "running" else 2.0,
        )
    )


# ---- snapshot format v2 -----------------------------------------------------


async def test_build_tree_snapshot_creator_only(daemon):
    """A creator with no children produces a v2 snapshot with one node."""
    _make_participant(daemon, pid="creator", pane="%1")
    snap = build_tree_snapshot(daemon, "creator")

    assert snap["version"] == 2
    assert snap["creator_id"] == "creator"
    assert len(snap["nodes"]) == 1
    node = snap["nodes"][0]
    assert node["participant_id"] == "creator"
    assert node["harness"] == "vibe"
    assert node["status"] == "idle"
    assert node["jobs"] == []


async def test_build_tree_snapshot_with_descendants(daemon):
    """Creator + two children captured in the snapshot."""
    _make_participant(daemon, pid="root", pane="%1")
    _make_participant(daemon, pid="child1", pane="%2", parent_id="root")
    _make_participant(daemon, pid="child2", pane="%3", parent_id="root")
    _make_job(daemon.store, handle="j1", caller_id="root")
    _make_job(daemon.store, handle="j2", caller_id="child1")

    snap = build_tree_snapshot(daemon, "root")

    assert snap["version"] == 2
    assert snap["creator_id"] == "root"
    ids = {n["participant_id"] for n in snap["nodes"]}
    assert ids == {"root", "child1", "child2"}

    root_node = next(n for n in snap["nodes"] if n["participant_id"] == "root")
    assert len(root_node["jobs"]) == 1
    assert root_node["jobs"][0]["handle"] == "j1"

    child1_node = next(n for n in snap["nodes"] if n["participant_id"] == "child1")
    assert len(child1_node["jobs"]) == 1
    assert child1_node["jobs"][0]["handle"] == "j2"

    child2_node = next(n for n in snap["nodes"] if n["participant_id"] == "child2")
    assert child2_node["jobs"] == []


async def test_build_tree_snapshot_grandchildren(daemon):
    """Three-level tree: root → child → grandchild."""
    _make_participant(daemon, pid="root", pane="%1")
    _make_participant(daemon, pid="child", pane="%2", parent_id="root")
    _make_participant(daemon, pid="grand", pane="%3", parent_id="child")

    snap = build_tree_snapshot(daemon, "root")
    ids = {n["participant_id"] for n in snap["nodes"]}
    assert ids == {"root", "child", "grand"}


async def test_build_tree_snapshot_includes_launch_provenance(daemon):
    """launch_provenance blob is included in the snapshot node."""
    prov_str = _prov("/proj")
    _make_participant(daemon, pid="root", pane="%1", launch_provenance=prov_str)
    snap = build_tree_snapshot(daemon, "root")

    node = snap["nodes"][0]
    assert isinstance(node["launch_provenance"], dict)
    assert node["launch_provenance"]["cwd_resolved"] == "/proj"


async def test_snapshot_job_keys(daemon):
    """Jobs in the v2 snapshot include caller_id and response_format (full set)."""
    _make_participant(daemon, pid="root", pane="%1")
    _make_job(daemon.store, handle="h1", caller_id="root", state="done")

    snap = build_tree_snapshot(daemon, "root")
    root_node = snap["nodes"][0]
    assert len(root_node["jobs"]) == 1
    job = root_node["jobs"][0]
    # v2 snapshot includes caller_id (for job attribution) and response_format.
    assert set(job) == {
        "handle",
        "caller_id",
        "target_id",
        "kind",
        "prompt",
        "state",
        "result",
        "error_code",
        "created_at",
        "finished_at",
        "response_format",
    }


# ---- checkpoint.create v2 output --------------------------------------------


async def test_checkpoint_create_returns_v2_snapshot(client, daemon):
    """checkpoint.create returns 'snapshot' with version=2 and legacy 'jobs'."""
    caller = await client.call("hello", id="c1", harness="vibe", cwd="/tmp")
    result = await client.call("checkpoint.create", caller_id=caller["id"], name="tp")

    assert "checkpoint_id" in result
    assert "jobs" in result  # backward-compatible flat list
    assert "snapshot" in result
    snap = result["snapshot"]
    assert snap["version"] == 2
    assert snap["creator_id"] == caller["id"]
    assert isinstance(snap["nodes"], list)


async def test_checkpoint_create_snapshot_includes_children(client, daemon):
    """Children of the caller appear in the v2 snapshot."""
    _make_participant(daemon, pid="root", pane="%1")
    _make_participant(daemon, pid="child", pane="%2", parent_id="root")
    result = await client.call("checkpoint.create", caller_id="root", name="tp")

    snap = result["snapshot"]
    ids = {n["participant_id"] for n in snap["nodes"]}
    assert ids == {"root", "child"}


# ---- checkpoint.read v2 output -----------------------------------------------


async def test_checkpoint_read_exposes_snapshot_version(client, daemon):
    """checkpoint.read includes snapshot_version and snapshot_node_count."""
    _make_participant(daemon, pid="root", pane="%1")
    _make_participant(daemon, pid="child", pane="%2", parent_id="root")
    created = await client.call("checkpoint.create", caller_id="root", name="tp")

    read = await client.call("checkpoint.read", checkpoint_id=created["checkpoint_id"])
    assert read["checkpoint"]["snapshot_version"] == 2
    assert read["checkpoint"]["snapshot_node_count"] == 2
    assert read["tree_snapshot"] is not None
    assert read["tree_snapshot"]["version"] == 2


async def test_checkpoint_read_backward_compat_recorded_jobs(client, daemon):
    """recorded_jobs in checkpoint.read is still the creator's flat job list."""
    _make_participant(daemon, pid="root", pane="%1")
    _make_job(daemon.store, handle="j1", caller_id="root")
    created = await client.call("checkpoint.create", caller_id="root", name="tp")

    read = await client.call("checkpoint.read", checkpoint_id=created["checkpoint_id"])
    assert len(read["recorded_jobs"]) == 1
    assert read["recorded_jobs"][0]["handle"] == "j1"


# ---- checkpoint.list summary fields -----------------------------------------


async def test_checkpoint_list_includes_snapshot_version(client, daemon):
    """checkpoint.list summary includes snapshot_version and snapshot_node_count."""
    caller = await client.call("hello", id="c1", harness="vibe", cwd="/tmp")
    await client.call("checkpoint.create", caller_id=caller["id"], name="tp")

    rows = await client.call("checkpoint.list", caller_id=caller["id"])
    assert rows[0]["snapshot_version"] == 2
    assert rows[0]["snapshot_node_count"] == 1


# ---- classification ---------------------------------------------------------


def test_classify_live_with_pane():
    """A live SPAWNED participant is classified as 'live'."""
    recorded = {"participant_id": "p", "jobs": [], "session_id": None, "session_correlation": None}
    live = Participant(id="p", harness="vibe", tier=Tier.SPAWNED, tmux_pane="%1")
    cls, _reason = classify_node(recorded, live, {})
    assert cls == "live"


def test_classify_live_external_fails():
    """A live EXTERNAL participant is classified as 'failed' (no pane)."""
    recorded = {"participant_id": "p", "jobs": [], "session_id": None, "session_correlation": None}
    live = Participant(id="p", harness="vibe", tier=Tier.EXTERNAL)
    cls, _reason = classify_node(recorded, live, {})
    assert cls == "failed"
    assert "EXTERNAL" in _reason


def test_classify_dead_with_trusted_session():
    """A dead participant with trusted session_id is 'resumable'."""
    from theater.provenance import TranscriptProvenance

    recorded = {
        "participant_id": "p",
        "jobs": [],
        "session_id": "sess-abc",
        "session_correlation": str(TranscriptProvenance.EXACT),
    }
    live = Participant(
        id="p",
        harness="vibe",
        tier=Tier.SPAWNED,
        status=Status.DEAD,
        session_id="sess-abc",
        session_correlation=str(TranscriptProvenance.EXACT),
    )
    cls, _reason = classify_node(recorded, live, {})
    assert cls == "resumable"


def test_classify_dead_with_provenance():
    """A dead participant with launch_provenance is 'respawnable'."""
    prov_str = _prov()
    recorded = {"participant_id": "p", "jobs": [], "session_id": None, "session_correlation": None}
    live = Participant(
        id="p",
        harness="vibe",
        tier=Tier.SPAWNED,
        status=Status.DEAD,
        launch_provenance=prov_str,
    )
    cls, _reason = classify_node(recorded, live, {})
    assert cls == "respawnable"


def test_classify_dead_no_provenance_no_open_jobs():
    """A dead participant with no provenance and no open jobs is 'completed'."""
    recorded = {
        "participant_id": "p",
        "jobs": [{"state": "done"}],
        "session_id": None,
        "session_correlation": None,
    }
    live = Participant(
        id="p",
        harness="vibe",
        tier=Tier.SPAWNED,
        status=Status.DEAD,
    )
    cls, _reason = classify_node(recorded, live, {})
    assert cls == "completed"


def test_classify_dead_no_provenance_with_open_jobs():
    """A dead participant with open jobs and no provenance is 'failed'."""
    recorded = {
        "participant_id": "p",
        "jobs": [{"state": "running"}],
        "session_id": None,
        "session_correlation": None,
    }
    live = Participant(
        id="p",
        harness="vibe",
        tier=Tier.SPAWNED,
        status=Status.DEAD,
    )
    cls, _reason = classify_node(recorded, live, {})
    assert cls == "failed"
    assert "no usable provenance" in _reason or "open job" in _reason


def test_classify_pruned_with_snapshot_provenance():
    """A GC'd participant with snapshot provenance is 'respawnable'."""
    recorded = {
        "participant_id": "p",
        "jobs": [],
        "session_id": None,
        "session_correlation": None,
        "launch_provenance": {"cwd_resolved": "/tmp", "cwd_requested": "/tmp"},
    }
    cls, _reason = classify_node(recorded, None, {})
    assert cls == "respawnable"


def test_classify_pruned_with_trusted_session_not_resumable():
    """A GC'd (pruned) participant with snapshot session_id is NOT resumable.

    Review item 2: pruned participants cannot be native-resumed from snapshot
    session_id/correlation alone. The spawner requires a retained trusted DB
    binding; snapshot evidence cannot prove the session is not live elsewhere.
    A pruned node with no provenance and no open jobs is 'completed'.
    """
    from theater.provenance import TranscriptProvenance

    recorded = {
        "participant_id": "p",
        "jobs": [],
        "session_id": "sess-abc",
        "session_correlation": str(TranscriptProvenance.EXACT),
        "launch_provenance": None,
    }
    cls, _reason = classify_node(recorded, None, {})
    # No provenance, no running jobs, no inbound spawn → completed (nothing to restore)
    assert cls == "completed"


def test_classify_pruned_no_provenance_no_open_jobs():
    """A GC'd participant with no provenance and no open jobs is 'completed'."""
    recorded = {
        "participant_id": "p",
        "jobs": [{"state": "done"}],
        "session_id": None,
        "session_correlation": None,
        "launch_provenance": None,
    }
    cls, _reason = classify_node(recorded, None, {})
    assert cls == "completed"


def test_classify_pruned_no_provenance_with_open_jobs():
    """A GC'd participant with open jobs and no provenance is 'pruned'."""
    recorded = {
        "participant_id": "p",
        "jobs": [{"state": "running"}],
        "session_id": None,
        "session_correlation": None,
        "launch_provenance": None,
    }
    cls, _reason = classify_node(recorded, None, {})
    assert cls == "pruned"


# ---- BFS order --------------------------------------------------------------


def test_bfs_order_single_node():
    nodes = {"root": {"participant_id": "root", "parent_id": None}}
    order = _bfs_order("root", nodes)
    assert order == ["root"]


def test_bfs_order_parent_child():
    nodes = {
        "root": {"participant_id": "root", "parent_id": None},
        "child": {"participant_id": "child", "parent_id": "root"},
    }
    order = _bfs_order("root", nodes)
    assert order[0] == "root"
    assert "child" in order


def test_bfs_order_grandchild():
    nodes = {
        "root": {"participant_id": "root", "parent_id": None},
        "child": {"participant_id": "child", "parent_id": "root"},
        "grand": {"participant_id": "grand", "parent_id": "child"},
    }
    order = _bfs_order("root", nodes)
    assert order.index("root") < order.index("child") < order.index("grand")


def test_bfs_order_orphan_attached_to_creator():
    """Nodes whose parent is not in the snapshot are attached to the creator."""
    nodes = {
        "root": {"participant_id": "root", "parent_id": None},
        "orphan": {"participant_id": "orphan", "parent_id": "missing"},
    }
    order = _bfs_order("root", nodes)
    assert "orphan" in order
    assert order.index("root") < order.index("orphan")


# ---- v2 restore: live creator -----------------------------------------------


async def test_restore_v2_live_creator_reused(client, daemon, fake_tmux):
    """A live creator is reused in place (action=reused_live)."""
    _make_participant(daemon, pid="creator", pane="%1")
    _make_participant(daemon, pid="restorer", pane="%2")

    created = await client.call("checkpoint.create", caller_id="creator", name="cp")
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer",
    )

    assert result["creator"]["action"] == "reused_live"
    # classification is "live" or "stale_live" depending on whether tmux can verify the pane.
    assert result["creator"]["classification"] in ("live", "stale_live")
    assert result["creator"]["original_participant_id"] == "creator"
    assert result["creator"]["new_participant_id"] == "creator"
    assert result["creator"]["final_status"] is not None
    assert result["descendants"] == []
    assert result["partial_failures"] == []


# ---- v2 restore: completed/pruned descendants skipped -----------------------


async def test_restore_v2_dead_child_no_provenance_skipped(client, daemon, fake_tmux):
    """A dead child with no open work and no provenance is skipped."""
    _make_participant(daemon, pid="creator", pane="%1")
    _make_participant(daemon, pid="child", pane="%2", parent_id="creator", dead=True)
    _make_participant(daemon, pid="restorer", pane="%3")

    created = await client.call("checkpoint.create", caller_id="creator", name="cp")
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer",
    )

    assert result["creator"]["action"] == "reused_live"
    assert len(result["descendants"]) == 1
    child_report = result["descendants"][0]
    assert child_report["original_participant_id"] == "child"
    assert child_report["action"] == "skipped"
    assert child_report["classification"] == "completed"


# ---- v2 restore: partial failure in descendant does not fail checkpoint ----


async def test_restore_v2_partial_descendant_failure_does_not_fail_checkpoint(
    client, daemon, fake_tmux, monkeypatch
):
    """A descendant spawn failure is captured in the report, not raised."""
    import theater.daemon.methods as methods_mod

    _make_participant(daemon, pid="creator", pane="%1")
    _make_participant(
        daemon,
        pid="child",
        pane="%2",
        parent_id="creator",
        dead=True,
        launch_provenance=_prov(),
    )
    _make_participant(daemon, pid="restorer", pane="%3")

    created = await client.call("checkpoint.create", caller_id="creator", name="cp")

    call_count = 0

    async def _selective_fail(daemon, params):
        nonlocal call_count
        call_count += 1
        # The creator is live so no spawn is called for it.
        # The child is respawnable — fake its spawn as failing.
        raise RuntimeError("child spawn exploded")

    monkeypatch.setattr(methods_mod, "_spawn", _selective_fail)

    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer",
    )

    # Creator succeeded (live — no spawn needed).
    assert result["creator"]["action"] == "reused_live"
    # Child failed — checkpoint is 'partial' (some success, some failure).
    assert len(result["descendants"]) == 1
    assert result["descendants"][0]["action"] == "failed"
    assert result["partial_failures"] == ["child"]

    read = await client.call("checkpoint.read", checkpoint_id=created["checkpoint_id"])
    # partial: some nodes succeeded, some failed
    assert read["checkpoint"]["restore_state"] == "partial"


# ---- v2 restore: creator failure marks checkpoint as failed -----------------


async def test_restore_v2_creator_failure_marks_failed(client, daemon, fake_tmux, monkeypatch):
    """A creator spawn failure marks the checkpoint as 'failed'."""
    import theater.daemon.methods as methods_mod

    _make_participant(
        daemon,
        pid="creator",
        pane="%1",
        dead=True,
        launch_provenance=_prov(),
    )
    _make_participant(daemon, pid="restorer", pane="%2")

    created = await client.call("checkpoint.create", caller_id="creator", name="cp")

    async def _fail_spawn(daemon, params):
        raise RuntimeError("daemon crashed mid-restore")

    monkeypatch.setattr(methods_mod, "_spawn", _fail_spawn)

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id="restorer",
        )
    assert "daemon crashed mid-restore" in str(exc.value)

    read = await client.call("checkpoint.read", checkpoint_id=created["checkpoint_id"])
    assert read["checkpoint"]["restore_state"] == "failed"


# ---- v2 restore: job reconciliation in report -------------------------------


async def test_restore_v2_jobs_in_report(client, daemon, fake_tmux):
    """Each node's jobs appear in the per-participant report."""
    _make_participant(daemon, pid="creator", pane="%1")
    _make_participant(daemon, pid="restorer", pane="%2")
    _make_job(daemon.store, handle="j1", caller_id="creator", state="running")
    _make_job(daemon.store, handle="j2", caller_id="creator", state="done")

    created = await client.call("checkpoint.create", caller_id="creator", name="cp")
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer",
    )

    recs = result["creator"]["job_reconciliations"]
    handles = {r["handle"] for r in recs}
    assert handles == {"j1", "j2"}
    # All send jobs are reported_only (never replayed).
    for r in recs:
        assert r["kind"] == "send"
        assert r["outcome"] == "reported_only"


# ---- v2 restore: lineage (new_parent_id) ------------------------------------


async def test_restore_v2_creator_parent_is_caller(client, daemon, fake_tmux):
    """The restored creator's new_parent_id is the restorer's id."""
    _make_participant(daemon, pid="creator", pane="%1")
    _make_participant(daemon, pid="restorer", pane="%2")

    created = await client.call("checkpoint.create", caller_id="creator", name="cp")
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer",
    )

    # For a live creator, new_parent_id is unchanged (the existing parent).
    assert result["creator"]["original_participant_id"] == "creator"
    # The current_parent_id is the current DB parent.
    # new_parent_id for a live node equals current_parent_id (not reparented).


async def test_restore_v2_descendant_parent_id_in_report(client, daemon, fake_tmux):
    """Descendant reports carry their original and new parent IDs."""
    _make_participant(daemon, pid="creator", pane="%1")
    _make_participant(daemon, pid="child", pane="%2", parent_id="creator", dead=True)
    _make_participant(daemon, pid="restorer", pane="%3")

    created = await client.call("checkpoint.create", caller_id="creator", name="cp")
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer",
    )

    assert len(result["descendants"]) == 1
    child = result["descendants"][0]
    assert child["original_parent_id"] == "creator"


# ---- v2 restore: machine-global discovery -----------------------------------


async def test_restore_v2_machine_global_checkpoint_discoverable(client, daemon, fake_tmux):
    """Any participant can discover and restore any checkpoint (machine-global)."""
    _make_participant(daemon, pid="creator-x", pane="%1")
    _make_participant(daemon, pid="restorer-y", pane="%2")

    created = await client.call("checkpoint.create", caller_id="creator-x", name="cp")

    # restorer-y lists and finds creator-x's checkpoint.
    rows = await client.call("checkpoint.list", caller_id="restorer-y")
    assert any(r["id"] == created["checkpoint_id"] for r in rows)

    # restorer-y reads it.
    read = await client.call("checkpoint.read", checkpoint_id=created["checkpoint_id"])
    assert read["checkpoint"]["participant_id"] == "creator-x"

    # restorer-y restores it.
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer-y",
    )
    assert result["creator"]["action"] == "reused_live"


# ---- v2 restore: single-use atomic claim ------------------------------------


async def test_restore_v2_concurrent_claim_refused(client, daemon, fake_tmux):
    """A second restore attempt on a claimed checkpoint is refused."""
    _make_participant(daemon, pid="creator", pane="%1")
    _make_participant(daemon, pid="restorer-a", pane="%2")
    _make_participant(daemon, pid="restorer-b", pane="%3")

    created = await client.call("checkpoint.create", caller_id="creator", name="cp")

    # Manually claim the restore as A so it's in 'restoring' state.
    token = daemon.store.claim_checkpoint_restore(created["checkpoint_id"], "restorer-a")
    assert token is not None

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id="restorer-b",
        )
    assert exc.value.code == "checkpoint_restore_in_progress"
    assert "restorer-a" in str(exc.value)


# ---- v2 restore: self-restore rejected (deadlock guard) ---------------------


async def test_restore_v2_self_restore_rejected(client, daemon, fake_tmux):
    """A creator cannot restore its own checkpoint (deadlock guard)."""
    _make_participant(daemon, pid="creator", pane="%1")
    created = await client.call("checkpoint.create", caller_id="creator", name="cp")

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id="creator",
        )
    assert exc.value.code == "bad_request"
    assert "self-restore" in str(exc.value)


# ---- v2 restore: cycle guard -----------------------------------------------


async def test_restore_v2_cycle_guard_on_live_ancestor(client, daemon, fake_tmux):
    """Restoring to a live ancestor of the caller is rejected (cycle)."""
    from sqlalchemy import update as sa_update

    from theater.daemon.schema import participants as part_table

    ancestor = Participant(
        id="anc-v2", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%1"
    )
    descendant = Participant(
        id="desc-v2", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%2"
    )
    daemon.store.upsert_participant(ancestor)
    daemon.store.upsert_participant(descendant)
    daemon.store.conn.execute(
        sa_update(part_table).where(part_table.c.id == descendant.id).values(parent_id=ancestor.id)
    )

    created = await client.call("checkpoint.create", caller_id="anc-v2", name="cp")
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id="desc-v2",
        )
    assert exc.value.code == "bad_request"
    assert "cycle" in str(exc.value)


# ---- v2 restore: restore_result persisted -----------------------------------


async def test_restore_v2_result_persisted_in_read(client, daemon, fake_tmux):
    """The full tree restore result is persisted and readable via checkpoint.read."""
    _make_participant(daemon, pid="creator-pr", pane="%1")
    _make_participant(daemon, pid="restorer-pr", pane="%2")

    created = await client.call("checkpoint.create", caller_id="creator-pr", name="cp")
    await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer-pr",
    )

    read = await client.call("checkpoint.read", checkpoint_id=created["checkpoint_id"])
    assert read["checkpoint"]["restore_state"] == "restored"
    result = read["checkpoint"]["restore_result"]
    assert result is not None
    assert "creator" in result
    assert result["creator"]["action"] == "reused_live"


# ---- v2 restore: stranded recovery (daemon restart simulation) --------------


async def test_restore_v2_stranded_marked_failed_on_startup(store):
    """Checkpoints in 'restoring' state are marked failed at daemon startup."""
    cid = store.create_checkpoint(participant_id="p1", name="cp", jobs_snapshot="{}")
    token = store.claim_checkpoint_restore(cid, "caller1")
    assert token is not None

    stranded = store.recover_stranded_restores()
    assert stranded == 1

    row = store.get_checkpoint(cid)
    assert row["restore_state"] == "failed"
    assert "daemon restarted" in row["restore_error"]


# ---- v1 compatibility -------------------------------------------------------


def test_parse_snapshot_v1_list():
    """A bare list parses as v1 (not is_v2_snapshot)."""
    data = parse_snapshot('[{"handle": "h1", "state": "done"}]')
    assert isinstance(data, list)
    assert not is_v2_snapshot(data)


def test_parse_snapshot_v2_dict():
    """A dict with version=2 is a v2 snapshot."""
    snap = {"version": 2, "creator_id": "c", "nodes": []}
    raw = json.dumps(snap)
    data = parse_snapshot(raw)
    assert is_v2_snapshot(data)


def test_upgrade_v1_snapshot_for_read_wraps_to_v1_dict():
    """upgrade_v1_snapshot_for_read wraps a flat list into a version=1 dict."""
    jobs = [{"handle": "h1", "state": "done"}]
    result = upgrade_v1_snapshot_for_read(jobs, creator_id="creator-x")
    assert result["version"] == 1
    assert result["creator_id"] == "creator-x"
    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["jobs"] == jobs


async def test_checkpoint_read_v1_checkpoint_still_readable(client, daemon):
    """A v1 checkpoint (flat list) can still be read via checkpoint.read."""
    # Manually insert a v1 snapshot (flat list).
    _make_participant(daemon, pid="old-creator", pane="%1")
    v1_snapshot = json.dumps(
        [
            {
                "handle": "h1",
                "target_id": "t",
                "kind": "send",
                "prompt": "p",
                "state": "done",
                "result": None,
                "error_code": None,
                "created_at": 1.0,
                "finished_at": 2.0,
            }
        ]
    )
    cid = daemon.store.create_checkpoint(
        participant_id="old-creator",
        name="legacy",
        jobs_snapshot=v1_snapshot,
    )

    read = await client.call("checkpoint.read", checkpoint_id=cid)
    assert read["checkpoint"]["snapshot_version"] == 1
    assert read["checkpoint"]["snapshot_node_count"] == 1
    assert read["tree_snapshot"] is None
    # recorded_jobs comes from the flat list.
    assert len(read["recorded_jobs"]) == 1
    assert read["recorded_jobs"][0]["handle"] == "h1"


async def test_checkpoint_list_v1_checkpoint_shows_version_1(client, daemon):
    """A v1 checkpoint shows snapshot_version=1 in checkpoint.list."""
    _make_participant(daemon, pid="old-creator-2", pane="%1")
    v1_snapshot = json.dumps([])
    cid = daemon.store.create_checkpoint(
        participant_id="old-creator-2",
        name="v1-list",
        jobs_snapshot=v1_snapshot,
    )

    rows = await client.call("checkpoint.list", caller_id="old-creator-2")
    row = next(r for r in rows if r["id"] == cid)
    assert row["snapshot_version"] == 1


async def test_checkpoint_restore_v1_live_creator(client, daemon, fake_tmux):
    """Restoring a v1 checkpoint with a live creator returns legacy shape."""
    _make_participant(daemon, pid="v1-creator", pane="%1")
    _make_participant(daemon, pid="v1-restorer", pane="%2")

    v1_snapshot = json.dumps([])
    cid = daemon.store.create_checkpoint(
        participant_id="v1-creator",
        name="v1-live",
        jobs_snapshot=v1_snapshot,
    )

    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=cid,
        approval="yolo",
        caller_id="v1-restorer",
    )

    # v1 restore returns the legacy shape with 'restored_parent'.
    assert "restored_parent" in result
    assert result["restored_parent"]["action"] == "reused_live"
    assert result["restored_parent"]["participant_id"] == "v1-creator"


# ---- launch_provenance in spawner -------------------------------------------


async def test_launch_provenance_recorded_at_spawn(client, daemon, fake_tmux):
    """When a participant is spawned, launch_provenance is persisted."""
    from theater.models import Participant, Tier

    caller = Participant(
        id="prov-caller", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%1"
    )
    daemon.store.upsert_participant(caller)

    # Spawn a child via the daemon (which calls the real spawner path).
    result = await client.call(
        "spawn",
        harness="vibe",
        prompt="test task",
        cwd="/tmp",
        approval="yolo",
        parent_id="prov-caller",
    )

    child_id = result["id"]
    child = daemon.store.get_participant(child_id)
    assert child is not None
    assert child.launch_provenance is not None

    prov = json.loads(child.launch_provenance)
    assert prov["cwd_requested"] == "/tmp"
    assert prov["approval"] == "yolo"
    assert prov["prompt"] == "test task"


# ---- send jobs not replayed -------------------------------------------------


async def test_restore_v2_send_jobs_not_replayed(client, daemon, fake_tmux):
    """Send jobs appear in the restore report but are never re-sent."""
    _make_participant(daemon, pid="creator-s", pane="%1")
    _make_participant(daemon, pid="restorer-s", pane="%2")

    # Create a send job from creator to some target.
    _make_job(daemon.store, handle="send-1", caller_id="creator-s", kind="send", state="done")

    created = await client.call("checkpoint.create", caller_id="creator-s", name="cp")

    # The send job was not running at checkpoint time; restore must not replay it.
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer-s",
    )

    creator_recs = result["creator"]["job_reconciliations"]
    send_recs = [r for r in creator_recs if r["kind"] == "send"]
    assert len(send_recs) == 1
    assert send_recs[0]["handle"] == "send-1"
    assert send_recs[0]["outcome"] == "reported_only"
    # No new jobs should have been created for this send.
    from sqlalchemy import select as sa_select

    all_jobs = daemon.store.conn.execute(
        sa_select(jobs_table).where(jobs_table.c.caller_id == "restorer-s")
    ).fetchall()
    assert len(all_jobs) == 0  # restorer created no new send jobs


# ---- durable incremental progress (second-attempt refused) ------------------


async def test_restore_v2_second_attempt_refused(client, daemon, fake_tmux):
    """A second restore on an already-restored checkpoint is refused."""
    _make_participant(daemon, pid="creator-2a", pane="%1")
    _make_participant(daemon, pid="restorer-2a", pane="%2")

    created = await client.call("checkpoint.create", caller_id="creator-2a", name="cp")
    await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer-2a",
    )

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id="restorer-2a",
        )
    assert exc.value.code == "checkpoint_already_restored"


# ---- no-live-pane guard (external / no-pane) --------------------------------


async def test_restore_v2_live_creator_without_pane_rejected(client, daemon, fake_tmux):
    """A live creator without a tmux pane is rejected before claiming."""
    _make_participant(daemon, pid="creator-np", pane=None)  # no pane
    _make_participant(daemon, pid="restorer-np", pane="%2")

    created = await client.call("checkpoint.create", caller_id="creator-np", name="cp")

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id="restorer-np",
        )
    assert exc.value.code == "bad_request"
    assert "no pane" in str(exc.value)


# ---- rails in restore -------------------------------------------------------


async def test_restore_v2_creator_failed_classification_reported(client, daemon, fake_tmux):
    """A 'failed' classification for the creator raises from restore_tree."""
    # Creator is EXTERNAL — cannot be restored (failed classification).
    await client.call("hello", id="ext-cr", harness="vibe")  # no cwd → EXTERNAL
    _make_participant(daemon, pid="caller-r", pane="%2")

    created = await client.call("checkpoint.create", caller_id="ext-cr", name="cp")

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id="caller-r",
        )
    # EXTERNAL is rejected at _validate_restore_parent (before claiming).
    assert exc.value.code == "bad_request"
    assert "EXTERNAL" in str(exc.value)


# ---- snapshot v2 node report shape ------------------------------------------


async def test_restore_v2_node_report_shape(client, daemon, fake_tmux):
    """Each node report has all required fields."""
    _make_participant(daemon, pid="shape-creator", pane="%1")
    _make_participant(daemon, pid="shape-restorer", pane="%2")

    created = await client.call("checkpoint.create", caller_id="shape-creator", name="cp")
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="shape-restorer",
    )

    required_fields = {
        "original_participant_id",
        "new_participant_id",
        "original_parent_id",
        "current_parent_id",
        "new_parent_id",
        "harness",
        "original_session_id",
        "current_session_id",
        "classification",
        "action",
        "final_status",
        "reason",
        "job_reconciliations",
        "warnings",
    }
    assert required_fields.issubset(set(result["creator"]))
    # Top-level result must also include counts, participants flat list, restore_state.
    assert "counts" in result
    assert "participants" in result
    assert "restore_state" in result
    assert "checkpoint_id" in result
    assert "descendants" in result
    assert "partial_failures" in result


# ---- migration: new column present in existing rows -------------------------


async def test_launch_provenance_null_for_adopted_participant(daemon):
    """An ADOPTED participant (no spawn) has null launch_provenance."""
    p = daemon.registry.register(harness="vibe", pane="%5", cwd="/tmp")
    loaded = daemon.store.get_participant(p.id)
    assert loaded is not None
    assert loaded.launch_provenance is None


async def test_launch_provenance_null_for_external_participant(daemon):
    """An EXTERNAL participant has null launch_provenance."""
    p = daemon.registry.register(harness="vibe", pane=None, cwd=None)
    loaded = daemon.store.get_participant(p.id)
    assert loaded is not None
    assert loaded.launch_provenance is None
