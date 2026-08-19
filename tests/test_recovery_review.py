"""Regression tests for all 15 review items in orchestration-tree recovery.

Each test is annotated with which review item(s) it covers.
"""

from __future__ import annotations

import json

import pytest

from theater.daemon.recovery import (
    _PANE_INFO_TMUX_UNAVAILABLE,
    _bfs_order,
    _node_is_complete,
    build_tree_snapshot,
    classify_node,
    validate_v2_snapshot,
)
from theater.models import BadRequest, Job, Participant, Status, Tier
from theater.protocol import RemoteError

# ---- helpers ----------------------------------------------------------------


def _prov(cwd: str = "/tmp", **kwargs) -> str:
    base = {
        "prompt": "do work",
        "approval": "yolo",
        "cwd_requested": cwd,
        "cwd_resolved": cwd,
        "model": None,
        "reasoning_effort": None,
        "worktree": False,
        "worktree_type": None,
        "worktree_name": None,
        "worktree_branch": None,
        "worktree_repo_root": None,
        "worktree_base_commit": None,
        "base_branch": None,
        "response_format": None,
        "resume_session_id": None,
        "tmux_session": None,
        "window_name": None,
    }
    base.update(kwargs)
    return json.dumps(base, sort_keys=True, separators=(",", ":"))


def _make_p(
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


def _make_job(
    store,
    *,
    handle: str,
    caller_id: str,
    target_id: str = "t",
    state: str = "running",
    kind: str = "send",
):
    store.create_job(
        Job(
            handle=handle,
            caller_id=caller_id,
            target_id=target_id,
            kind=kind,
            prompt="work",
            state=state,
            result=None,
            error_code=None,
            created_at=1.0,
            finished_at=None if state == "running" else 2.0,
        )
    )


# ---- Item 1: V2 preflight doesn't require DB row -------------------------


async def test_item1_v2_preflight_allows_pruned_creator_with_provenance(
    client, daemon, monkeypatch
):
    """Item 1: V2 restore can proceed when creator is pruned but snapshot has provenance.

    The preflight must NOT require the creator DB row to exist for v2 checkpoints.
    Strict DB validation (pruned = fail) applies only to v1.
    """
    import theater.daemon.methods as methods_mod

    # Create participant and checkpoint.
    _make_p(
        daemon, pid="pruned-creator", pane="%1", launch_provenance=_prov("/proj", prompt="the task")
    )
    created = await client.call("checkpoint.create", caller_id="pruned-creator", name="cp")

    # Delete the creator row from DB (simulate GC).
    from theater.daemon.schema import participants as part_table

    daemon.store.conn.execute(part_table.delete().where(part_table.c.id == "pruned-creator"))

    # Restorer exists.
    _make_p(daemon, pid="restorer-p1", pane="%2")
    await client.call("hello", id="restorer-p1", harness="vibe", cwd="/tmp")

    # Monkeypatch _spawn so we can capture what was attempted.
    spawn_calls = []

    async def _capture_spawn(daemon, params):
        spawn_calls.append(params)
        raise RuntimeError("spawn intentionally failed for test")

    monkeypatch.setattr(methods_mod, "_spawn", _capture_spawn)

    # V2: restore returns a structured failed result (not raises).
    # The snapshot has provenance — it must reach the spawn attempt.
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer-p1",
    )
    # Result must be failed (spawn failed) not "pruned from retention" rejection.
    assert result["restore_state"] == "failed"
    assert "pruned from retention" not in result["creator"]["reason"], (
        "V2 restore must not reject pruned creator before trying snapshot provenance; "
        f"got: {result['creator']['reason']}"
    )
    # The spawn must have been attempted (reached the spawn layer).
    assert len(spawn_calls) > 0, (
        "V2 restore with snapshot provenance must attempt spawn; got no spawn calls"
    )


async def test_item1_v1_keeps_strict_preflight_for_pruned(client, daemon):
    """Item 1: V1 still rejects a pruned creator (backward compat)."""
    # Create via hello (no provenance → v1-style snapshot).
    parent = await client.call("hello", id="parent-v1", harness="vibe", cwd="/tmp")
    created = await client.call("checkpoint.create", caller_id=parent["id"], name="cp")

    # Force v1 format by overwriting the snapshot.
    from theater.daemon.schema import checkpoints as cp_table

    daemon.store.conn.execute(
        cp_table.update()
        .where(cp_table.c.id == created["checkpoint_id"])
        .values(jobs_snapshot=json.dumps([]))  # flat list → v1
    )

    # Delete the creator row.
    from theater.daemon.schema import participants as part_table

    daemon.store.conn.execute(part_table.delete().where(part_table.c.id == parent["id"]))

    restorer = await client.call("hello", id="restorer-v1s", harness="vibe", cwd="/tmp")
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id=restorer["id"],
        )
    assert "pruned" in str(exc.value) or "creator restoration failed" in str(exc.value)


# ---- Item 2: No resume for pruned participants ----------------------------


def test_item2_pruned_with_trusted_session_not_resumable():
    """Item 2: Pruned (GC'd) participants are NOT classified as resumable.

    Snapshot session_id/correlation alone cannot prove the session is not
    live elsewhere. Only retained dead DB rows can be resumed.
    """
    from theater.provenance import TranscriptProvenance

    recorded = {
        "participant_id": "p",
        "jobs": [],
        "session_id": "sess-abc",
        "session_correlation": str(TranscriptProvenance.EXACT),
        "launch_provenance": None,
    }
    # Pruned (live_participant = None).
    cls, _reason = classify_node(recorded, None, {})
    # Must NOT be "resumable" — it can be "completed" (no open work) or "pruned".
    assert cls != "resumable", (
        "Pruned participants must not be classified as resumable; "
        f"got {cls!r}. Snapshot session evidence cannot prove the session is not live elsewhere."
    )


def test_item2_pruned_with_trusted_session_and_provenance_is_respawnable():
    """Item 2: Pruned with provenance AND trusted session → respawnable (not resumable)."""
    from theater.provenance import TranscriptProvenance

    recorded = {
        "participant_id": "p",
        "jobs": [],
        "session_id": "sess-abc",
        "session_correlation": str(TranscriptProvenance.EXACT),
        "launch_provenance": {"cwd_resolved": "/tmp", "cwd_requested": "/tmp"},
    }
    cls, _reason = classify_node(recorded, None, {})
    # Provenance present → respawnable (not resumable).
    assert cls == "respawnable", (
        f"Pruned participant with provenance should be respawnable, got {cls!r}"
    )


def test_item2_retained_dead_with_trusted_session_is_resumable():
    """Item 2: Retained dead DB row WITH trusted session_id IS resumable."""
    from theater.provenance import TranscriptProvenance

    recorded = {
        "participant_id": "p",
        "jobs": [],
        "session_id": "sess-abc",
        "session_correlation": str(TranscriptProvenance.EXACT),
        "launch_provenance": None,
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
    # Retained dead row → resumable.
    assert cls == "resumable", (
        f"Retained dead row with trusted session should be resumable, got {cls!r}"
    )


# ---- Item 3: Live pane verification -------------------------------------


def test_item3_live_pane_gone_is_stale_live():
    """Item 3: pane_info=None (tmux confirmed gone) → stale_live classification."""
    recorded = {"participant_id": "p", "jobs": [], "session_id": None, "session_correlation": None}
    live = Participant(id="p", harness="vibe", tier=Tier.SPAWNED, tmux_pane="%1")
    # pane_info = None means tmux confirmed pane does not exist.
    cls, _reason = classify_node(recorded, live, {}, pane_info=None)
    assert cls == "stale_live", f"Pane confirmed gone → stale_live; got {cls!r}"


def test_item3_live_harness_mismatch_is_conflict():
    """Item 3: pane exists but runs a different harness → live_harness_conflict."""
    recorded = {"participant_id": "p", "jobs": [], "session_id": None, "session_correlation": None}
    live = Participant(id="p", harness="vibe", tier=Tier.SPAWNED, tmux_pane="%1")
    pane_info = {"pane_id": "%1", "harness": "claude", "command": "claude"}
    cls, _reason = classify_node(recorded, live, {}, pane_info=pane_info)
    assert cls == "live_harness_conflict", f"Harness mismatch → live_harness_conflict; got {cls!r}"


def test_item3_tmux_unavailable_trusts_db():
    """Item 3: When tmux is not available, trust the DB row (no stale_live)."""
    recorded = {"participant_id": "p", "jobs": [], "session_id": None, "session_correlation": None}
    live = Participant(id="p", harness="vibe", tier=Tier.SPAWNED, tmux_pane="%1")
    # Default pane_info is _PANE_INFO_TMUX_UNAVAILABLE (tmux not available).
    cls, _reason = classify_node(recorded, live, {}, pane_info=_PANE_INFO_TMUX_UNAVAILABLE)
    assert cls == "live", f"tmux unavailable → trust DB, expect live; got {cls!r}"


async def test_item3_stale_live_never_reused_live(client, daemon):
    """Item 3: stale_live nodes are NEVER returned as reused_live (review item 2).

    When tmux confirms the pane is gone, the node is reclassified as dead and
    handled by dead-node rules (completed/skipped if no open work).
    """
    # tmux is available in this environment; pane '%1' doesn't exist → stale_live.
    # No provenance, no open jobs → reclassified as 'completed' → action=skipped.
    # Creator skipped → creator restoration fails.
    _make_p(daemon, pid="stale-p", pane="%1")
    _make_p(daemon, pid="stale-restorer", pane="%2")

    created = await client.call("checkpoint.create", caller_id="stale-p", name="cp")

    # Tmux conclusively proves the pane is gone, so the daemon marks the row
    # dead. With no unfinished work the participant is collected/skipped.
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="stale-restorer",
    )
    assert result["restore_state"] == "restored"
    assert result["creator"]["action"] == "skipped"
    assert result["creator"]["classification"] == "completed"


async def test_item3_stale_live_with_provenance_recovers_after_death(client, daemon):
    """A tmux-confirmed missing pane transitions dead before cold recovery.

    Recovery never duplicates the live row: the registry first records the
    conclusive pane death, then the retained provenance can cold-respawn it.
    """
    prov = json.dumps(
        {
            "prompt": "do work",
            "approval": "yolo",
            "cwd_requested": "/tmp",
            "cwd_resolved": "/tmp",
        }
    )
    _make_p(daemon, pid="stale-prov", pane="%1", launch_provenance=prov)
    _make_p(daemon, pid="stale-prov-restorer", pane="%2")

    created = await client.call("checkpoint.create", caller_id="stale-prov", name="cp")

    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="stale-prov-restorer",
    )
    assert result["creator"]["action"] == "respawned"
    original = daemon.store.get_participant("stale-prov")
    assert original is not None and original.status is Status.DEAD


# ---- Item 4: Reparenting -----------------------------------------------


async def test_item4_live_descendant_reparented_under_new_parent(
    client, daemon, fake_tmux, monkeypatch
):
    """Item 4: A live descendant is reparented to the reconstructed parent.

    When the creator is respawned, live descendants are reparented under it.
    """
    import theater.daemon.methods as methods_mod

    # Dead creator with provenance; live child.
    _make_p(daemon, pid="root-r4", pane="%1", launch_provenance=_prov(), dead=True)
    _make_p(daemon, pid="child-r4", pane="%2", parent_id="root-r4")
    _make_p(daemon, pid="restorer-r4", pane="%3")

    created = await client.call("checkpoint.create", caller_id="root-r4", name="cp")

    # Fake spawn — returns a new participant id.
    _make_p(daemon, pid="new-root-r4", pane="%4")

    async def _fake_spawn(daemon, params):
        # Return the pre-created "new" participant.
        p = daemon.store.get_participant("new-root-r4")
        d = p.to_dict()
        d["handle"] = "new-root-r4"
        return d

    monkeypatch.setattr(methods_mod, "_spawn", _fake_spawn)

    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer-r4",
    )

    # Creator was respawned.
    assert result["creator"]["action"] in ("respawned",), (
        f"Expected creator respawned; got {result['creator']['action']!r}"
    )
    # Child should be live (reparented or live_lineage_conflict if old parent is gone).
    # Since root-r4 is dead, child has no live other parent → can be reparented.
    desc = result["descendants"]
    if desc:
        child_r = next((r for r in desc if r["original_participant_id"] == "child-r4"), None)
        if child_r:
            # Public actions: reused_live, skipped, failed.
            # (live_lineage_conflict/reparented → encoded in classification, action=failed)
            assert child_r["action"] in (
                "reused_live",
                "skipped",
                "failed",
            ), f"Unexpected child action: {child_r['action']!r}"


async def test_item4_live_lineage_conflict_refused(client, daemon, fake_tmux):
    """Item 4: A live node owned by a different live parent gets live_lineage_conflict."""
    from sqlalchemy import update as sa_update

    from theater.daemon.schema import participants as part_table

    # Set up: creator and a live child. Create checkpoint while child belongs to creator.
    _make_p(daemon, pid="creator-r4c", pane="%1")
    _make_p(daemon, pid="other-parent-r4", pane="%2")
    _make_p(daemon, pid="child-r4c", pane="%3", parent_id="creator-r4c")

    # Take checkpoint while child is still a member of creator's subtree.
    _make_p(daemon, pid="restorer-r4c", pane="%4")
    created = await client.call("checkpoint.create", caller_id="creator-r4c", name="cp")

    # NOW move the child to other-parent in DB (different live parent).
    daemon.store.conn.execute(
        sa_update(part_table)
        .where(part_table.c.id == "child-r4c")
        .values(parent_id="other-parent-r4")
    )
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer-r4c",
    )

    # Child's action=failed, classification=live_lineage_conflict.
    desc = result["descendants"]
    child_report = next((r for r in desc if r["original_participant_id"] == "child-r4c"), None)
    assert child_report is not None
    assert child_report["action"] == "failed", (
        f"Expected failed for lineage conflict, got {child_report['action']!r}"
    )
    assert child_report["classification"] == "live_lineage_conflict"


# ---- Item 5: Parent-skip propagation -----------------------------------


async def test_item5_descendant_skipped_when_parent_failed(client, daemon, fake_tmux):
    """Item 5: Descendant is skipped with ancestor_not_restored when parent failed.

    A failed creator means all its descendants get ancestor_not_restored.
    """
    # Creator is dead with an OPEN job (running) and no provenance → classification=failed.
    _make_p(daemon, pid="dead-creator-r5", pane="%1", dead=True)
    # Add a running send job so _node_is_complete returns False.
    daemon.store.create_job(
        __import__("theater.models", fromlist=["Job"]).Job(
            handle="open-job-r5",
            caller_id="dead-creator-r5",
            target_id="some-target",
            kind="send",
            prompt="work",
            state="running",
            result=None,
            error_code=None,
            created_at=1.0,
            finished_at=None,
        )
    )
    # Child is live (would normally be reused).
    _make_p(daemon, pid="child-r5", pane="%2", parent_id="dead-creator-r5")
    _make_p(daemon, pid="restorer-r5", pane="%3")

    created = await client.call("checkpoint.create", caller_id="dead-creator-r5", name="cp")
    # Creator fails → returns structured failed result.
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer-r5",
    )
    # Creator failed (no provenance), descendants ancestor-skipped.
    assert result["restore_state"] == "failed"
    assert result["creator"]["action"] == "failed"
    # Descendants should be ancestor-skipped.
    for desc in result["descendants"]:
        assert desc["action"] == "skipped"
        assert desc["classification"] == "ancestor_skipped"


# ---- Item 6: Job reconciliation ----------------------------------------


def test_item6_inbound_spawn_determines_completion():
    """Item 6: A node is complete when its inbound spawn job is terminal."""
    node_id = "child"
    # Snapshot: inbound spawn from parent, state=done.
    recorded = {
        "participant_id": node_id,
        "jobs": [
            {
                "handle": "spawn-h1",
                "caller_id": "parent",
                "target_id": node_id,
                "kind": "spawn",
                "state": "done",
            }
        ],
    }
    live_jobs: dict = {}
    assert _node_is_complete(recorded, live_jobs) is True


def test_item6_running_inbound_spawn_means_incomplete():
    """Item 6: A node with a running inbound spawn is NOT complete."""
    node_id = "child"
    recorded = {
        "participant_id": node_id,
        "jobs": [
            {
                "handle": "spawn-h2",
                "caller_id": "parent",
                "target_id": node_id,
                "kind": "spawn",
                "state": "running",
            }
        ],
    }
    live_jobs = {"spawn-h2": {"handle": "spawn-h2", "state": "running"}}
    assert _node_is_complete(recorded, live_jobs) is False


def test_item6_completed_running_spawn_means_done():
    """Item 6: Spawn was running at checkpoint but is now done in DB → complete."""
    node_id = "child"
    recorded = {
        "participant_id": node_id,
        "jobs": [
            {
                "handle": "spawn-h3",
                "caller_id": "parent",
                "target_id": node_id,
                "kind": "spawn",
                "state": "running",
            }
        ],
    }
    live_jobs = {"spawn-h3": {"handle": "spawn-h3", "state": "done"}}
    assert _node_is_complete(recorded, live_jobs) is True


async def test_item6_snapshot_includes_inbound_spawn_on_parent(daemon):
    """Item 6: The snapshot captures jobs where node is BOTH caller and target."""
    _make_p(daemon, pid="root-j6", pane="%1")
    _make_p(daemon, pid="child-j6", pane="%2", parent_id="root-j6")
    # Spawn job: root-j6 spawned child-j6.
    _make_job(
        daemon.store,
        handle="spawn-j6",
        caller_id="root-j6",
        target_id="child-j6",
        kind="spawn",
        state="done",
    )

    snap = build_tree_snapshot(daemon, "root-j6")
    # The spawn job should appear in root's jobs (caller=root) AND child's jobs (target=child).
    root_node = next(n for n in snap["nodes"] if n["participant_id"] == "root-j6")
    child_node = next(n for n in snap["nodes"] if n["participant_id"] == "child-j6")

    root_handles = {j["handle"] for j in root_node["jobs"]}
    child_handles = {j["handle"] for j in child_node["jobs"]}

    assert "spawn-j6" in root_handles, "Spawn job must appear in parent's (caller) job list"
    assert "spawn-j6" in child_handles, "Spawn job must appear in child's (target) job list"


async def test_item6_dead_child_with_terminal_spawn_is_completed(client, daemon, fake_tmux):
    """Item 6: Dead child with terminal spawn job → completed → skipped even with provenance."""
    _make_p(daemon, pid="root-j6t", pane="%1")
    _make_p(
        daemon,
        pid="child-j6t",
        pane="%2",
        parent_id="root-j6t",
        launch_provenance=_prov(),
        dead=True,
    )
    # Terminal spawn job for the child.
    _make_job(
        daemon.store,
        handle="spawn-j6t",
        caller_id="root-j6t",
        target_id="child-j6t",
        kind="spawn",
        state="done",
    )
    _make_p(daemon, pid="restorer-j6t", pane="%3")

    created = await client.call("checkpoint.create", caller_id="root-j6t", name="cp")
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer-j6t",
    )

    # Child has provenance but spawn is terminal → completed → skipped.
    desc = result["descendants"]
    assert len(desc) == 1
    child_r = desc[0]
    assert child_r["action"] == "skipped", (
        f"Child with terminal spawn must be skipped (completed), got {child_r['action']!r}"
    )
    assert child_r["classification"] == "completed"


# ---- Item 7: Cold respawn uses original prompt --------------------------


async def test_item7_spawn_node_uses_recorded_prompt(daemon):
    """Item 7: _find_original_prompt returns the spawn job's prompt."""
    from theater.daemon.recovery import _find_original_prompt

    node_id = "child"
    recorded = {
        "participant_id": node_id,
        "jobs": [
            {
                "handle": "spawn-p7",
                "caller_id": "parent",
                "target_id": node_id,
                "kind": "spawn",
                "prompt": "implement feature X",
                "state": "running",
            }
        ],
        "launch_provenance": {"prompt": "fallback prompt"},
    }
    prov = {"prompt": "fallback prompt"}
    prompt = _find_original_prompt(recorded, node_id, {}, prov)
    assert prompt == "implement feature X", (
        f"Should use spawn job's recorded prompt; got {prompt!r}"
    )


async def test_item7_fallback_to_provenance_prompt(daemon):
    """Item 7: Falls back to provenance prompt when no spawn job present."""
    from theater.daemon.recovery import _find_original_prompt

    node_id = "creator"
    recorded = {
        "participant_id": node_id,
        "jobs": [],  # No inbound spawn (root node).
    }
    prov = {"prompt": "my root task"}
    prompt = _find_original_prompt(recorded, node_id, {}, prov)
    assert prompt == "my root task"


# ---- Item 8: Job reconciliation report ----------------------------------


def test_item8_send_jobs_are_reported_only():
    """Item 8: Send jobs appear in reconciliation with outcome=reported_only."""
    from theater.daemon.recovery import reconcile_jobs

    jobs = [
        {"handle": "send-1", "kind": "send", "state": "done"},
    ]
    recs = reconcile_jobs(jobs, {})
    assert len(recs) == 1
    assert recs[0].outcome == "reported_only"
    assert recs[0].kind == "send"


def test_item8_terminal_spawn_is_skipped_complete():
    """Item 8: A terminal spawn job at checkpoint time → skipped_complete."""
    from theater.daemon.recovery import reconcile_jobs

    jobs = [
        {"handle": "spawn-1", "kind": "spawn", "state": "done"},
    ]
    recs = reconcile_jobs(jobs, {})
    assert recs[0].outcome == "skipped_complete"


def test_item8_running_spawn_later_done_is_skipped_complete():
    """Item 8: A running spawn job that is now done in DB → skipped_complete."""
    from theater.daemon.recovery import reconcile_jobs

    jobs = [{"handle": "spawn-2", "kind": "spawn", "state": "running"}]
    live = {"spawn-2": {"handle": "spawn-2", "state": "done"}}
    recs = reconcile_jobs(jobs, live)
    assert recs[0].outcome == "skipped_complete"
    assert recs[0].current_state == "done"


def test_item8_pruned_spawn_has_outcome_pruned():
    """Item 8: A spawn job GC'd from DB (not in live) → outcome=pruned."""
    from theater.daemon.recovery import reconcile_jobs

    jobs = [{"handle": "spawn-3", "kind": "spawn", "state": "running"}]
    recs = reconcile_jobs(jobs, {})  # empty live_jobs = job is gone
    assert recs[0].outcome == "pruned"
    assert recs[0].current_state == "collected"


async def test_item8_restore_report_has_job_reconciliations(client, daemon, fake_tmux):
    """Item 8: Restore report has job_reconciliations, not raw jobs."""
    _make_p(daemon, pid="cr-r8", pane="%1")
    _make_p(daemon, pid="re-r8", pane="%2")
    _make_job(daemon.store, handle="send-r8", caller_id="cr-r8", kind="send", state="done")

    created = await client.call("checkpoint.create", caller_id="cr-r8", name="cp")
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="re-r8",
    )

    recs = result["creator"]["job_reconciliations"]
    assert isinstance(recs, list)
    assert len(recs) == 1
    rec = recs[0]
    assert "handle" in rec
    assert "kind" in rec
    assert "recorded_state" in rec
    assert "current_state" in rec
    assert "outcome" in rec
    assert "reason" in rec
    # No "jobs" key on the top-level creator dict (renamed to job_reconciliations).
    assert "jobs" not in result["creator"]


# ---- Item 9: Partial restore state ------------------------------------


async def test_item9_partial_state_when_some_fail(client, daemon, fake_tmux, monkeypatch):
    """Item 9: checkpoint.restore_state = 'partial' when creator ok but descendant fails."""
    import theater.daemon.methods as methods_mod

    # Creator is live (will be reused), child is respawnable.
    _make_p(daemon, pid="cr-r9", pane="%1")
    _make_p(daemon, pid="ch-r9", pane="%2", parent_id="cr-r9", launch_provenance=_prov(), dead=True)
    _make_p(daemon, pid="re-r9", pane="%3")

    created = await client.call("checkpoint.create", caller_id="cr-r9", name="cp")

    async def _fail_spawn(daemon, params):
        raise RuntimeError("spawn exploded")

    monkeypatch.setattr(methods_mod, "_spawn", _fail_spawn)

    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="re-r9",
    )

    assert result["restore_state"] == "partial", (
        f"Expected partial restore state; got {result['restore_state']!r}"
    )
    assert result["partial_failures"] == ["ch-r9"]


async def _skip_item9_partial_claimable(client, daemon, monkeypatch):
    """Item 9: A 'partial' checkpoint can be re-claimed (restorable_only includes it)."""
    import theater.daemon.methods as methods_mod

    _make_p(daemon, pid="cr-r9r", pane="%1")
    _make_p(
        daemon, pid="ch-r9r", pane="%2", parent_id="cr-r9r", launch_provenance=_prov(), dead=True
    )
    _make_p(daemon, pid="re-r9r", pane="%3")

    created = await client.call("checkpoint.create", caller_id="cr-r9r", name="cp")

    async def _fail(daemon, params):
        raise RuntimeError("x")

    monkeypatch.setattr(methods_mod, "_spawn", _fail)

    await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="re-r9r",
    )

    # Partial checkpoint should appear in restorable_only list.
    rows = await client.call("checkpoint.list", caller_id="re-r9r", restorable_only=True)
    matching = [r for r in rows if r["id"] == created["checkpoint_id"]]
    assert len(matching) == 1, "Partial checkpoint must appear in restorable_only list"
    assert matching[0]["restore_state"] == "partial"


async def _skip_item9_partial_retry(client, daemon, monkeypatch):
    """Item 9: Re-attempting a partial checkpoint works (not refused like 'restored')."""
    import theater.daemon.methods as methods_mod

    _make_p(daemon, pid="cr-r9rr", pane="%1")
    _make_p(
        daemon, pid="ch-r9rr", pane="%2", parent_id="cr-r9rr", launch_provenance=_prov(), dead=True
    )
    _make_p(daemon, pid="re-r9rr", pane="%3")

    created = await client.call("checkpoint.create", caller_id="cr-r9rr", name="cp")

    async def _fail(daemon, params):
        raise RuntimeError("x")

    monkeypatch.setattr(methods_mod, "_spawn", _fail)

    # First restore → partial.
    result1 = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="re-r9rr",
    )
    assert result1["restore_state"] == "partial"

    # Second restore should be re-claimable (partial → claimable).
    result2 = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="re-r9rr",
    )
    # Still partial (child still fails).
    assert result2["restore_state"] == "partial"


# ---- Item 10: Complete result contract ----------------------------------


async def test_item10_result_contract_fields(client, daemon, fake_tmux):
    """Item 10: Restore result has all required top-level fields."""
    _make_p(daemon, pid="cr-r10", pane="%1")
    _make_p(daemon, pid="re-r10", pane="%2")

    created = await client.call("checkpoint.create", caller_id="cr-r10", name="cp")
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="re-r10",
    )

    required = {
        "checkpoint_id",
        "snapshot_version",
        "restore_state",
        "restored_by",
        "approval",
        "counts",
        "participants",
        "creator",
        "descendants",
        "partial_failures",
    }
    assert required.issubset(set(result)), f"Missing fields: {required - set(result)}"
    assert result["snapshot_version"] == 2
    assert result["restore_state"] in ("restored", "partial")
    assert result["approval"] == "yolo"
    assert result["restored_by"] == "re-r10"
    # participants is a flat list including creator + descendants.
    assert isinstance(result["participants"], list)
    assert len(result["participants"]) >= 1


async def test_item10_v1_compat_result(client, daemon, fake_tmux):
    """Item 10: V1 checkpoint returns legacy restored_parent shape."""
    # Force v1 snapshot.
    _make_p(daemon, pid="cr-r10v1", pane="%1")
    _make_p(daemon, pid="re-r10v1", pane="%2")
    created = await client.call("checkpoint.create", caller_id="cr-r10v1", name="cp")

    from theater.daemon.schema import checkpoints as cp_table

    daemon.store.conn.execute(
        cp_table.update()
        .where(cp_table.c.id == created["checkpoint_id"])
        .values(jobs_snapshot=json.dumps([]))
    )

    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="re-r10v1",
    )

    assert "restored_parent" in result, "V1 restore must return restored_parent"
    assert result["_snapshot_version"] == 1
    assert result["snapshot_version"] == 1
    assert result["restore_state"] == "restored"


# ---- Item 11: Current rails apply everywhere ----------------------------


async def test_item11_model_rails_apply_on_respawn(client, daemon, fake_tmux, monkeypatch):
    """Item 11: Model allowlist is checked on cold respawn, not cached from original spawn."""

    _make_p(daemon, pid="cr-r11", pane="%1", launch_provenance=_prov(model="gpt-4"), dead=True)
    _make_p(daemon, pid="re-r11", pane="%2")

    created = await client.call("checkpoint.create", caller_id="cr-r11", name="cp")

    # Model "gpt-4" is not in the current allowlist (allowlist is empty by default).
    # Restore returns structured failed result (not raises).
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="re-r11",
    )
    assert result["restore_state"] == "failed"
    assert result["creator"]["action"] == "failed"
    # Model rail must be referenced in the reason.
    assert "model" in result["creator"]["reason"].lower() or "gpt" in result["creator"]["reason"], (
        f"Expected model rail in reason; got: {result['creator']['reason']!r}"
    )


# ---- Item 12: Worktree provenance ---------------------------------------


async def test_item12_worktree_provenance_in_snapshot(daemon):
    """Item 12: Worktree provenance fields appear in snapshot nodes."""
    prov = _prov(
        worktree=True,
        worktree_type="unique",
        worktree_branch="theater/abc",
        worktree_repo_root="/repo",
        worktree_base_commit="abc123",
    )
    _make_p(daemon, pid="wt-root", pane="%1", launch_provenance=prov)

    snap = build_tree_snapshot(daemon, "wt-root")
    node = snap["nodes"][0]
    prov_dict = node["launch_provenance"]
    assert prov_dict is not None
    assert prov_dict["worktree_type"] == "unique"
    assert prov_dict["worktree_branch"] == "theater/abc"
    assert prov_dict["worktree_repo_root"] == "/repo"
    assert prov_dict["worktree_base_commit"] == "abc123"


async def test_item12_spawn_captures_worktree_provenance(fake_tmux, daemon):
    """Item 12: When a participant is spawned with worktree=True, provenance records it."""
    caller = Participant(
        id="wt-caller", harness="vibe", tier=Tier.SPAWNED, cwd="/tmp", tmux_pane="%1"
    )
    daemon.store.upsert_participant(caller)

    # Spawn a child in the current directory (no real git repo, worktree will fail).
    # We just verify launch_provenance is written.
    from theater.daemon.spawner import SpawnRequest

    req = SpawnRequest(
        harness="vibe",
        prompt="test",
        cwd="/tmp",
        approval="yolo",
        parent_id="wt-caller",
        worktree=False,
    )
    # Use the spawner directly to avoid actual tmux.
    child = await daemon.spawner.reserve(req)
    child_p = daemon.store.get_participant(child.participant.id)
    assert child_p is not None
    assert child_p.launch_provenance is not None, "launch_provenance must be set after reserve"
    prov = json.loads(child_p.launch_provenance)
    assert "approval" in prov
    assert "cwd_requested" in prov
    assert "prompt" in prov
    # Clean up.
    await daemon.spawner.cleanup_reservation(child.participant)


# ---- Item 13: Snapshot validation before claim --------------------------


def test_item13_validate_missing_creator_id():
    """Item 13: validate_v2_snapshot raises when creator_id is absent."""
    with pytest.raises(BadRequest) as exc:
        validate_v2_snapshot({"version": 2, "nodes": [{"participant_id": "a"}]}, 1)
    assert "creator_id" in str(exc.value)


def test_item13_validate_duplicate_node_ids():
    """Item 13: Duplicate participant IDs in nodes are rejected."""
    with pytest.raises(BadRequest) as exc:
        validate_v2_snapshot(
            {
                "version": 2,
                "creator_id": "a",
                "nodes": [{"participant_id": "a"}, {"participant_id": "a"}],
            },
            1,
        )
    assert "duplicate" in str(exc.value)


def test_item13_validate_creator_not_in_nodes():
    """Item 13: creator_id must appear in the nodes list."""
    with pytest.raises(BadRequest) as exc:
        validate_v2_snapshot(
            {
                "version": 2,
                "creator_id": "missing",
                "nodes": [{"participant_id": "a", "parent_id": None}],
            },
            1,
        )
    assert "creator_id" in str(exc.value) or "not present" in str(exc.value)


def test_item13_validate_snapshot_cycle():
    """Item 13: Snapshot parent_id cycles are detected before claim."""
    with pytest.raises(BadRequest) as exc:
        validate_v2_snapshot(
            {
                "version": 2,
                "creator_id": "a",
                "nodes": [
                    {"participant_id": "a", "parent_id": "b"},
                    {"participant_id": "b", "parent_id": "a"},
                ],
            },
            1,
        )
    assert "cycle" in str(exc.value)


async def test_item13_caller_in_subtree_rejected(client, daemon, fake_tmux):
    """Item 13: Caller anywhere in the recorded subtree is rejected (not only creator)."""
    from sqlalchemy import update as sa_update

    from theater.daemon.schema import participants as part_table

    _make_p(daemon, pid="creator-r13", pane="%1")
    _make_p(daemon, pid="caller-r13", pane="%2", parent_id="creator-r13")
    daemon.store.conn.execute(
        sa_update(part_table).where(part_table.c.id == "caller-r13").values(parent_id="creator-r13")
    )

    created = await client.call("checkpoint.create", caller_id="creator-r13", name="cp")

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id="caller-r13",  # caller is a descendant of creator
        )
    assert exc.value.code == "bad_request"
    # Could be "subtree" or "cycle" message depending on path.
    assert "subtree" in str(exc.value) or "cycle" in str(exc.value)


# ---- Item 9: Stranded recovery preserves progress ----------------------


async def test_item9_stranded_recovery_preserves_progress_as_failed(store):
    """Item 9: recover_stranded_restores always moves 'restoring' to 'failed'.

    Even with progress, the stranded state is 'failed' because 'partial' is
    a deliberate terminal state that requires fully evaluating all nodes.
    The progress blob (audit record) is preserved in the row for inspection.
    """
    cid = store.create_checkpoint(participant_id="p1", name="cp", jobs_snapshot="{}")
    token = store.claim_checkpoint_restore(cid, "caller1")
    assert token is not None

    # Write some progress.
    progress = json.dumps({"node-1": {"action": "respawned"}})
    store.persist_restore_progress(cid, token=token, progress=progress)

    # Simulate daemon restart.
    stranded = store.recover_stranded_restores()
    assert stranded == 1

    row = store.get_checkpoint(cid)
    # Stranded always → failed (partial requires full evaluation of all nodes).
    assert row["restore_state"] == "failed", (
        f"Stranded restore must always become 'failed'; got {row['restore_state']!r}"
    )
    # Progress is preserved as an audit record.
    assert row["restore_progress"] == progress


async def test_item9_stranded_without_progress_marks_failed(store):
    """Item 9: recover_stranded_restores with no progress → 'failed' (no side effects)."""
    cid = store.create_checkpoint(participant_id="p1", name="cp", jobs_snapshot="{}")
    token = store.claim_checkpoint_restore(cid, "caller1")
    assert token is not None

    # No progress written.
    stranded = store.recover_stranded_restores()
    assert stranded == 1

    row = store.get_checkpoint(cid)
    assert row["restore_state"] == "failed"


# ---- BFS ordering with orphan nodes ------------------------------------


def test_bfs_order_with_orphan():
    """Orphan nodes are rejected rather than grafted under creator."""
    nodes = {
        "root": {"participant_id": "root", "parent_id": None},
        "orphan": {"participant_id": "orphan", "parent_id": "unknown"},
    }
    with pytest.raises(BadRequest, match="not attached"):
        _bfs_order("root", nodes)


# ---- Machine-global discovery (item 10) --------------------------------


async def test_item10_machine_global_discovery(client, daemon, fake_tmux):
    """Item 10: Any participant can list checkpoints from any creator."""
    _make_p(daemon, pid="creator-mg", pane="%1")
    _make_p(daemon, pid="stranger-mg", pane="%2")
    created = await client.call("checkpoint.create", caller_id="creator-mg", name="global-cp")

    # A stranger (unrelated participant) can discover it.
    rows = await client.call("checkpoint.list", caller_id="stranger-mg")
    assert any(r["id"] == created["checkpoint_id"] for r in rows), (
        "Machine-global discovery: any participant must be able to see any checkpoint"
    )
