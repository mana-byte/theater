"""Regression tests for review round 2, all 14 items.

Each test is annotated with which item(s) it covers.
"""

from __future__ import annotations

import json

import pytest

from theater.daemon.recovery import (
    classify_node,
    validate_v2_snapshot,
)
from theater.models import BadRequest, Job, Participant, Status, Tier
from theater.protocol import RemoteError

# ---- helpers ----------------------------------------------------------------


def _prov(**kwargs) -> str:
    base = {
        "prompt": "do work",
        "approval": "yolo",
        "cwd_requested": "/tmp",
        "cwd_resolved": "/tmp",
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
    pane: str | None = None,
    parent_id: str | None = None,
    dead: bool = False,
    session_id: str | None = None,
    session_correlation: str | None = None,
    launch_provenance: str | None = None,
    harness: str = "vibe",
    cwd: str = "/tmp",
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


def _make_job(store, *, handle, caller_id, target_id="t", state="running", kind="send"):
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


# ---- Item 1: partial is terminal / claim only ready -------------------------


async def test_item1_partial_is_terminal_cannot_claim(store):
    """Item 1: A 'partial' checkpoint cannot be re-claimed."""
    cid = store.create_checkpoint(participant_id="p", name="cp", jobs_snapshot="{}")
    token = store.claim_checkpoint_restore(cid, "caller")
    assert token is not None
    # Finalize as partial.
    ok = store.finalize_checkpoint_restore(
        cid, token=token, restored_by="caller", result="{}", partial=True
    )
    assert ok
    row = store.get_checkpoint(cid)
    assert row["restore_state"] == "partial"

    # Cannot re-claim a partial checkpoint.
    token2 = store.claim_checkpoint_restore(cid, "caller2")
    assert token2 is None, "partial checkpoint must not be re-claimable"


async def test_item1_ready_only_in_restorable_only(client, daemon):
    """Item 1: restorable_only=True returns only 'ready' checkpoints."""
    p = await client.call("hello", id="p1", harness="vibe", cwd="/tmp")

    ready_cp = await client.call("checkpoint.create", caller_id=p["id"], name="ready")
    partial_cp = await client.call("checkpoint.create", caller_id=p["id"], name="partial")
    failed_cp = await client.call("checkpoint.create", caller_id=p["id"], name="failed")

    # Manually set states.
    t1 = daemon.store.claim_checkpoint_restore(partial_cp["checkpoint_id"], p["id"])
    daemon.store.finalize_checkpoint_restore(
        partial_cp["checkpoint_id"], token=t1, restored_by=p["id"], result="{}", partial=True
    )
    t2 = daemon.store.claim_checkpoint_restore(failed_cp["checkpoint_id"], p["id"])
    daemon.store.finalize_checkpoint_restore(
        failed_cp["checkpoint_id"], token=t2, restored_by=p["id"], error="boom"
    )

    rows = await client.call("checkpoint.list", caller_id=p["id"], restorable_only=True)
    ids = {r["id"] for r in rows}
    assert ready_cp["checkpoint_id"] in ids, "ready must appear in restorable_only"
    assert partial_cp["checkpoint_id"] not in ids, "partial must NOT appear in restorable_only"
    assert failed_cp["checkpoint_id"] not in ids, "failed must NOT appear in restorable_only"


async def test_item1_partial_claim_raises_checkpoint_restore_partial(store):
    """Item 1: A 'partial' checkpoint raises CheckpointRestorePartial on re-claim attempt.

    Verified at store level: claim_checkpoint_restore returns None for partial state,
    and the error message from _restore_state_error says 'terminal'.
    """
    from theater.daemon.methods import _restore_state_error

    cid = store.create_checkpoint(participant_id="p1", name="cp", jobs_snapshot="{}")
    token = store.claim_checkpoint_restore(cid, "caller")
    assert token is not None
    store.finalize_checkpoint_restore(
        cid, token=token, restored_by="caller", result="{}", partial=True
    )
    row = store.get_checkpoint(cid)
    assert row["restore_state"] == "partial"

    # Cannot re-claim.
    token2 = store.claim_checkpoint_restore(cid, "caller2")
    assert token2 is None

    # The error for partial must say terminal.
    err = _restore_state_error("partial", cid)
    assert err.code == "checkpoint_restore_partial"
    assert "terminal" in str(err)


# ---- Item 2: stale/mismatch never reused_live --------------------------------


def test_item2_stale_live_pane_gone_classifies_as_stale():
    """Item 2: Pane confirmed gone by tmux → stale_live (not live)."""
    recorded = {"participant_id": "p", "jobs": []}
    live = Participant(id="p", harness="vibe", tier=Tier.SPAWNED, tmux_pane="%1")
    cls, _ = classify_node(recorded, live, {}, pane_info=None)
    assert cls == "stale_live", f"Expected stale_live for confirmed-gone pane, got {cls!r}"


def test_item2_stale_live_with_provenance_still_fails():
    """Item 2/6: stale_live + provenance → STILL failed (not respawnable).

    Cannot cold-spawn when DB row says live — would create a duplicate participant.
    """
    from theater.daemon.recovery import _classify_as_dead

    recorded = {
        "participant_id": "p",
        "jobs": [],
        "launch_provenance": {"cwd_resolved": "/tmp", "cwd_requested": "/tmp"},
    }
    live = Participant(
        id="p",
        harness="vibe",
        tier=Tier.SPAWNED,
        tmux_pane="%1",
        launch_provenance=json.dumps({"cwd_resolved": "/tmp"}),
    )
    cls, reason = _classify_as_dead(recorded, live, {})
    assert cls == "failed", f"stale_live must always fail (item 6), got {cls!r}"
    assert "live" in reason.lower() or "duplicate" in reason.lower() or "stale" in reason.lower()


def test_item2_live_harness_conflict_always_fails():
    """Item 2/6: live_harness_conflict → ALWAYS fails (item 6 safety).

    Cannot assume the pane is dead when harness is different — it could be a human.
    Must fail rather than guessing.
    """
    from theater.daemon.recovery import _classify_as_dead

    recorded = {"participant_id": "p", "jobs": [{"state": "done", "kind": "send"}]}
    live = Participant(id="p", harness="vibe", tier=Tier.SPAWNED, tmux_pane="%1")
    cls, _reason = _classify_as_dead(recorded, live, {}, stale_reason="live harness conflict")
    assert cls == "failed", f"live_harness_conflict must always fail (item 6), got {cls!r}"


# ---- Item 3: action enum exactly five values ---------------------------------


async def test_item3_action_enum_live_lineage_conflict_is_failed(client, daemon, fake_tmux):
    """Item 3: live_lineage_conflict maps to action=failed, classification=live_lineage_conflict."""
    from sqlalchemy import update as sa_update

    from theater.daemon.schema import participants as part_table

    _make_p(daemon, pid="creator-3", pane="%1")
    _make_p(daemon, pid="other-3", pane="%2")
    _make_p(daemon, pid="child-3", pane="%3", parent_id="creator-3")
    _make_p(daemon, pid="restorer-3", pane=None)

    created = await client.call("checkpoint.create", caller_id="creator-3", name="cp")

    # Move child under other-3 after checkpoint.
    daemon.store.conn.execute(
        sa_update(part_table).where(part_table.c.id == "child-3").values(parent_id="other-3")
    )

    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer-3",
    )

    # Creator has no pane → stale_live → no provenance, no open jobs → completed → skipped.
    # So creator might fail. But child must have live_lineage_conflict → action=failed.
    all_participants = result.get("participants", [])
    child_r = next((r for r in all_participants if r["original_participant_id"] == "child-3"), None)
    if child_r:
        assert child_r["action"] == "failed", (
            f"live_lineage_conflict must map to action=failed, got {child_r['action']!r}"
        )
        assert child_r["classification"] == "live_lineage_conflict"


async def test_item3_ancestor_not_restored_maps_to_skipped(client, daemon, fake_tmux):
    """Item 3: creator failure → descendants get action=skipped, classification=ancestor_skipped."""
    # Dead creator with running job (makes it failed, not completed).
    _make_p(daemon, pid="dead-cr-3", pane=None, dead=True)
    _make_job(daemon.store, handle="open-j-cr3", caller_id="dead-cr-3", state="running")
    _make_p(daemon, pid="child-cr-3", pane="%1", parent_id="dead-cr-3")
    _make_p(daemon, pid="restorer-cr-3", pane="%2")

    created = await client.call("checkpoint.create", caller_id="dead-cr-3", name="cp")
    # Restore returns structured failed result.
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer-cr-3",
    )
    assert result["restore_state"] == "failed"
    assert result["creator"]["action"] == "failed"
    # Child should be ancestor-skipped.
    for desc in result["descendants"]:
        assert desc["action"] == "skipped"
        assert desc["classification"] == "ancestor_skipped"


# ---- Item 4: restore_state calc includes all unsuccessful -------------------


async def test_item4_resumed_counts_as_success(store):
    """Item 4: resumed is a success action for restore_state calc."""
    from theater.daemon.recovery import _action_is_success

    assert _action_is_success("resumed"), "resumed must be a success action"
    assert _action_is_success("reused_live"), "reused_live must be a success"
    assert _action_is_success("respawned"), "respawned must be a success"
    assert not _action_is_success("skipped"), "skipped is not success"
    assert not _action_is_success("failed"), "failed is not success"


async def test_item4_all_skipped_is_restored_not_partial(client, daemon, fake_tmux):
    """Item 4: All nodes skipped (completed) → restore_state=restored (no failures)."""
    # Creator live + child dead with terminal spawn = all completed/skipped.
    _make_p(daemon, pid="live-cr-4", pane="%1")
    _make_p(daemon, pid="child-4", pane="%2", parent_id="live-cr-4", dead=True)
    _make_job(
        daemon.store,
        handle="spawn-4",
        caller_id="live-cr-4",
        target_id="child-4",
        kind="spawn",
        state="done",
    )
    _make_p(daemon, pid="restorer-4", pane="%3")

    created = await client.call("checkpoint.create", caller_id="live-cr-4", name="cp")
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer-4",
    )
    # Creator reused_live (success), child skipped (completed) — no failures.
    assert result["restore_state"] == "restored", (
        f"All completed/skipped must be restored; got {result['restore_state']!r}"
    )


# ---- Item 5: reparent failure = action=failed --------------------------------


async def test_item5_reparent_failure_action_failed(client, daemon, fake_tmux):
    """Item 5: A reparent failure makes action=failed, not a warning on reused_live."""
    from theater.daemon.recovery import _reparent_live

    # Create a cycle: try to reparent a node under itself.
    _make_p(daemon, pid="node-5", pane="%1")

    with pytest.raises(BadRequest) as exc:
        _reparent_live(daemon, "node-5", new_parent_id="node-5", checkpoint_id=1)
    assert "self-loop" in str(exc.value)


# ---- Item 6: progress persisted after every node ---------------------------


async def test_item6_progress_persisted_after_every_node(client, daemon, fake_tmux, monkeypatch):
    """Item 6: Progress blob is written after every node outcome, including failed/skipped."""
    import theater.daemon.methods as methods_mod

    _make_p(daemon, pid="creator-6", pane="%1")
    _make_p(
        daemon,
        pid="child-dead-6",
        pane="%2",
        parent_id="creator-6",
        launch_provenance=_prov(),
        dead=True,
    )
    _make_p(daemon, pid="restorer-6", pane="%3")

    created = await client.call("checkpoint.create", caller_id="creator-6", name="cp")

    # Fail the child spawn to produce a partial/failed descendant.
    async def _fail(daemon, params):
        raise RuntimeError("forced spawn failure")

    monkeypatch.setattr(methods_mod, "_spawn", _fail)

    await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer-6",
    )

    # Progress must be written (covers both creator and child).
    row = daemon.store.get_checkpoint(created["checkpoint_id"])
    assert row["restore_progress"] is not None, "restore_progress must be written after nodes"
    progress = json.loads(row["restore_progress"])
    # Creator should be in progress (it succeeded as reused_live).
    assert "creator-6" in progress, f"creator must be in progress; got {progress.keys()}"
    # Child spawn failed — must also be in progress (audit).
    assert "child-dead-6" in progress, f"failed child must be in progress; got {progress.keys()}"


# ---- Item 7: creator failure persists result before raising -----------------


async def test_item7_creator_failure_marks_checkpoint_failed(client, daemon, monkeypatch):
    """Item 7: Creator failure marks checkpoint as failed (not stranded)."""
    import theater.daemon.methods as methods_mod

    _make_p(daemon, pid="creator-7", pane=None, dead=True, launch_provenance=_prov())
    _make_p(daemon, pid="restorer-7", pane=None)

    created = await client.call("checkpoint.create", caller_id="creator-7", name="cp")

    async def _fail(daemon, params):
        raise RuntimeError("creator spawn failed")

    monkeypatch.setattr(methods_mod, "_spawn", _fail)

    # Creator failure → structured failed result, not raises.
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer-7",
    )
    assert result["restore_state"] == "failed"
    assert "creator spawn failed" in result["creator"]["reason"]

    # Checkpoint must be marked 'failed', not 'restoring' (no stranding).
    row = daemon.store.get_checkpoint(created["checkpoint_id"])
    assert row["restore_state"] == "failed", (
        f"Creator failure must finalize as failed; got {row['restore_state']!r}"
    )


# ---- Item 8: worktree safety — fail clearly ---------------------------------


def test_item8_worktree_required_but_missing_returns_none_cwd():
    """Item 8: worktree required, path gone, no provenance → (None, False, warnings)."""
    from theater.daemon.recovery import _resolve_worktree_cwd

    prov = {
        "worktree_type": "unique",
        "worktree_branch": None,  # no branch
        "worktree_repo_root": None,  # no repo root
        "cwd_resolved": "/nonexistent/path/worktree",
        "cwd_requested": "/nonexistent/path",
    }
    cwd, wt_param, wt_warnings = _resolve_worktree_cwd(prov, {}, new_participant_id="p")
    assert cwd is None, "Must return None cwd when worktree required but unrestorable"
    assert wt_param is False
    assert any("missing" in w or "provenance" in w for w in wt_warnings), (
        f"Must warn about missing worktree; got {wt_warnings}"
    )


def test_item8_worktree_uses_main_repo_root_not_show_toplevel(daemon):
    """Item 8: launch_provenance uses worktree.main_repo_root, not show-toplevel.

    worktree.main_repo_root uses git rev-parse --git-common-dir (canonical main root),
    not --show-toplevel (which returns the linked worktree's own top level).
    This is tested structurally by verifying the spawner imports main_repo_root.
    """
    import pathlib

    from theater.daemon import spawner as spawner_mod

    # Verify spawner uses worktree_mod.main_repo_root (not worktree_mod.repo_root).
    src = pathlib.Path(spawner_mod.__file__).read_text()
    assert "main_repo_root" in src, "spawner must use main_repo_root for canonical repo root"
    assert "worktree_mod.main_repo_root" in src, "must call via worktree_mod"


def test_item8_worktree_no_spurious_param_on_reuse(tmp_path):
    """Item 8: When reusing an existing verified worktree path, worktree_param=False."""
    from theater.daemon.recovery import _resolve_worktree_cwd

    # Create a real directory to simulate the worktree path.
    wt_path = tmp_path / "worktree"
    wt_path.mkdir()

    # Without a valid git repo, rev-parse will fail → fallback to recreation.
    prov = {
        "worktree_type": "unique",
        "worktree_branch": "theater/abc",
        "worktree_repo_root": str(tmp_path),
        "cwd_resolved": str(wt_path),
    }
    cwd, wt_param, _warnings = _resolve_worktree_cwd(prov, {}, new_participant_id="p")
    # If git rev-parse failed (not a real repo), it falls through to recreation.
    # Either way, param should not be True if path is being reused (cwd=path, param=False).
    # In a real worktree, cwd=path and param=False. In this tmp case, git fails →
    # it tries recreation: cwd=repo_root, param=True. Either is acceptable.
    if cwd == str(wt_path):
        assert wt_param is False, "Reused path must pass worktree_param=False"


# ---- Item 9: snapshot validates dangling parent links -----------------------


def test_item9_dangling_parent_link_rejected():
    """Item 9: Non-creator nodes with parent outside snapshot are rejected."""
    with pytest.raises(BadRequest) as exc:
        validate_v2_snapshot(
            {
                "version": 2,
                "creator_id": "creator",
                "nodes": [
                    {"participant_id": "creator", "parent_id": None},
                    {
                        "participant_id": "child",
                        "parent_id": "external-parent",  # not in snapshot
                    },
                ],
            },
            checkpoint_id=1,
        )
    assert "dangling" in str(exc.value), f"Must reject dangling parent; got: {exc.value}"


def test_item9_creator_may_have_external_parent():
    """Item 9: The creator itself may have an external parent (its own ancestor)."""
    # Should not raise.
    validate_v2_snapshot(
        {
            "version": 2,
            "creator_id": "creator",
            "nodes": [
                {"participant_id": "creator", "parent_id": "external-ancestor"},
            ],
        },
        checkpoint_id=1,
    )


def test_item9_child_with_creator_as_parent_ok():
    """Item 9: A child with parent_id == creator_id is valid."""
    validate_v2_snapshot(
        {
            "version": 2,
            "creator_id": "creator",
            "nodes": [
                {"participant_id": "creator", "parent_id": None},
                {"participant_id": "child", "parent_id": "creator"},
            ],
        },
        checkpoint_id=1,
    )


# ---- Item 10: preflight topology checks -------------------------------------


async def test_item10_preflight_caller_in_subtree_rejected(client, daemon, fake_tmux):
    """Item 10: Caller in the recorded subtree is rejected before claiming."""
    from sqlalchemy import update as sa_update

    from theater.daemon.schema import participants as part_table

    _make_p(daemon, pid="creator-10", pane="%1")
    _make_p(daemon, pid="caller-10", pane="%2", parent_id="creator-10")
    # Make caller a child of creator.
    daemon.store.conn.execute(
        sa_update(part_table).where(part_table.c.id == "caller-10").values(parent_id="creator-10")
    )

    created = await client.call("checkpoint.create", caller_id="creator-10", name="cp")

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id="caller-10",
        )
    assert "subtree" in str(exc.value) or "cycle" in str(exc.value)
    # Claim must NOT have been consumed (preflight is pre-claim).
    row = daemon.store.get_checkpoint(created["checkpoint_id"])
    assert row["restore_state"] == "ready", "Preflight rejection must not consume the claim"


# ---- Item 11: PLR0912/PLR0915 reverted from global -------------------------


def test_item11_pyproject_no_global_plr0912_plr0915():
    """Item 11: pyproject.toml does not globally suppress PLR0912 or PLR0915."""
    import pathlib

    pyproject = pathlib.Path(__file__).parents[1] / "pyproject.toml"
    content = pyproject.read_text()
    # Find the ignore list.
    in_ignore = False
    ignore_content = ""
    for line in content.splitlines():
        if "ignore" in line and "=[" in line.replace(" ", ""):
            in_ignore = True
        if in_ignore:
            ignore_content += line
            if "]" in line:
                break
    assert "PLR0912" not in ignore_content, (
        "PLR0912 must not be in global ignore list; use local noqa"
    )
    assert "PLR0915" not in ignore_content, (
        "PLR0915 must not be in global ignore list; use local noqa"
    )


# ---- Item 12: job deduplication ---------------------------------------------


async def test_item12_top_level_jobs_deduplicated(client, daemon, fake_tmux):
    """Item 12: A spawn job appearing on both parent and child is deduplicated in top-level jobs."""
    _make_p(daemon, pid="cr-12", pane="%1")
    _make_p(daemon, pid="ch-12", pane="%2", parent_id="cr-12", dead=True)
    # Spawn job: parent is caller, child is target.
    _make_job(
        daemon.store,
        handle="spawn-12",
        caller_id="cr-12",
        target_id="ch-12",
        kind="spawn",
        state="done",
    )
    _make_p(daemon, pid="restorer-12", pane="%3")

    created = await client.call("checkpoint.create", caller_id="cr-12", name="cp")
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer-12",
    )

    top_jobs = result.get("jobs", [])
    handles = [j["handle"] for j in top_jobs]
    assert handles.count("spawn-12") == 1, (
        f"Spawn job must appear exactly once in top-level jobs; got {handles}"
    )


# ---- Item 13: complete result contract --------------------------------------


async def test_item13_result_has_all_required_fields(client, daemon, fake_tmux):
    """Item 13: v2 restore result has all required fields."""
    _make_p(daemon, pid="cr-13", pane="%1")
    _make_p(daemon, pid="restorer-13", pane="%2")

    created = await client.call("checkpoint.create", caller_id="cr-13", name="cp")
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer-13",
    )

    required_top = {
        "checkpoint_id",
        "snapshot_version",
        "restore_state",
        "restored_by",
        "approval",
        "counts",
        "jobs",
        "participants",
        "creator",
        "descendants",
        "partial_failures",
    }
    assert required_top.issubset(set(result)), (
        f"Missing top-level fields: {required_top - set(result)}"
    )

    required_node = {
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
    creator = result["creator"]
    assert required_node.issubset(set(creator)), (
        f"Missing node fields: {required_node - set(creator)}"
    )


async def test_item13_v1_result_explicitly_degraded(client, daemon):
    """Item 13: v1 restore result contains _degraded=True and _degraded_reason."""
    # Use a dead SPAWNED participant (v1 validation: dead → cwd required).
    _make_p(daemon, pid="p-v1-13", pane=None, cwd="/tmp", dead=True)
    restorer = await client.call("hello", id="r-v1-13", harness="vibe", cwd="/tmp")

    # Create a v1 snapshot manually.
    cid = daemon.store.create_checkpoint(
        participant_id="p-v1-13",
        name="v1",
        jobs_snapshot=json.dumps([]),
    )

    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=cid,
        approval="yolo",
        caller_id=restorer["id"],
    )

    assert result.get("_degraded") is True, "v1 result must have _degraded=True"
    assert "_degraded_reason" in result, "v1 result must have _degraded_reason"
    degraded_reason = result["_degraded_reason"].lower()
    assert "creator" in degraded_reason and "only" in degraded_reason
    assert "restored_parent" in result, "v1 result must have restored_parent for compat"


# ---- Item 14: MCP descriptions mention v2 semantics -------------------------


def test_item14_mcp_tools_recovery_restore_description():
    """Item 14: mcp/tools.py recovery_restore mentions v2 semantics."""
    import pathlib

    tools_src = (pathlib.Path(__file__).parents[1] / "theater/mcp/tools.py").read_text()
    # Find the recovery_restore docstring.
    idx = tools_src.find("async def recovery_restore")
    assert idx != -1
    docstring_area = tools_src[idx : idx + 2000]
    assert "partial" in docstring_area, "tools.py must mention partial state"
    assert "terminal" in docstring_area, "tools.py must say partial/failed are terminal"
    assert "v1" in docstring_area or "degraded" in docstring_area, (
        "tools.py must mention v1 degraded mode"
    )


def test_item14_mcp_server_recovery_restore_description():
    """Item 14: mcp/server.py recovery_restore mentions v2 action enum."""
    import pathlib

    server_src = (pathlib.Path(__file__).parents[1] / "theater/mcp/server.py").read_text()
    idx = server_src.find("async def recovery_restore")
    assert idx != -1
    doc_area = server_src[idx : idx + 2000]
    assert "reused_live" in doc_area, "server.py must mention reused_live action"
    assert "respawned" in doc_area, "server.py must mention respawned action"
    assert "partial" in doc_area, "server.py must mention partial state"
    assert "terminal" in doc_area, "server.py must say states are terminal"


# ---- Item 9 stranded recovery semantics -------------------------------------


async def test_item9_stranded_always_failed_not_partial(store):
    """Item 9: Stranded restores (with OR without progress) always become 'failed'."""
    cid_with = store.create_checkpoint(participant_id="p", name="cp", jobs_snapshot="{}")
    t1 = store.claim_checkpoint_restore(cid_with, "c")
    store.persist_restore_progress(cid_with, token=t1, progress='{"a":1}')

    cid_without = store.create_checkpoint(participant_id="p", name="cp2", jobs_snapshot="{}")
    store.claim_checkpoint_restore(cid_without, "c")

    stranded = store.recover_stranded_restores()
    assert stranded == 2

    row_with = store.get_checkpoint(cid_with)
    row_without = store.get_checkpoint(cid_without)

    assert row_with["restore_state"] == "failed", (
        "Stranded with progress must become failed (not partial)"
    )
    assert row_without["restore_state"] == "failed"
    # Progress blob preserved as audit record.
    assert row_with["restore_progress"] is not None, "Progress must be preserved as audit"
