"""Adversarial regressions from checkpoint tree-recovery review round three."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from theater.config import RailsSection
from theater.daemon.recovery import (
    _spawn_node,
    preflight_topology,
    validate_v2_snapshot,
)
from theater.models import BadRequest, Job, Participant, Status, Tier
from theater.protocol import RemoteError


def _participant(
    daemon,
    pid: str,
    *,
    parent_id: str | None = None,
    pane: str | None = None,
    dead: bool = False,
    provenance: dict | None = None,
) -> Participant:
    participant = Participant(
        id=pid,
        harness="vibe",
        tier=Tier.SPAWNED,
        cwd="/tmp",
        parent_id=parent_id,
        tmux_pane=pane,
        launch_provenance=json.dumps(provenance) if provenance is not None else None,
    )
    daemon.store.upsert_participant(participant)
    if dead:
        daemon.store.set_status(pid, Status.DEAD)
    return participant


def _job(
    daemon,
    handle: str,
    *,
    caller_id: str,
    target_id: str,
    kind: str = "spawn",
    state: str = "running",
    prompt: str = "do work",
) -> None:
    daemon.store.create_job(
        Job(
            handle=handle,
            caller_id=caller_id,
            target_id=target_id,
            kind=kind,
            prompt=prompt,
            state=state,
            result=None,
            error_code=None,
            created_at=1.0,
            finished_at=None if state == "running" else 2.0,
        )
    )


def _snapshot_node(
    pid: str,
    parent_id: str | None,
    *,
    provenance: dict | None = None,
) -> dict:
    return {
        "participant_id": pid,
        "harness": "vibe",
        "tier": "spawned",
        "status": "dead",
        "parent_id": parent_id,
        "session_id": None,
        "session_correlation": None,
        "cwd": "/tmp",
        "branch": None,
        "launch_provenance": provenance,
        "jobs": [],
    }


def test_non_creator_null_parent_is_rejected() -> None:
    snapshot = {
        "version": 2,
        "creator_id": "creator",
        "nodes": [
            _snapshot_node("creator", None),
            _snapshot_node("orphan", None),
        ],
    }
    with pytest.raises(BadRequest, match="parent_id=null"):
        validate_v2_snapshot(snapshot, 7)


def test_preflight_projects_pruned_multilevel_depth(daemon) -> None:
    _participant(daemon, "caller")
    provenance = {"cwd_requested": "/tmp", "cwd_resolved": "/tmp"}
    nodes = [_snapshot_node("creator", None, provenance=provenance)]
    parent = "creator"
    for index in range(3):
        child = f"child-{index}"
        nodes.append(_snapshot_node(child, parent, provenance=provenance))
        parent = child
    snapshot = {"version": 2, "creator_id": "creator", "nodes": nodes}

    with pytest.raises(BadRequest, match="depth"):
        preflight_topology(daemon, checkpoint_id=8, snapshot=snapshot, caller_id="caller")


def test_preflight_counts_post_checkpoint_live_descendants(daemon) -> None:
    daemon.config = replace(
        daemon.config,
        rails=RailsSection(depth_cap=10, budget=3),
    )
    provenance = {"cwd_requested": "/tmp", "cwd_resolved": "/tmp"}
    _participant(daemon, "caller")
    _participant(daemon, "creator", dead=True, provenance=provenance)
    _participant(daemon, "recorded-child", parent_id="creator", pane="%1")
    _participant(daemon, "late-child", parent_id="recorded-child", pane="%2")

    snapshot = {
        "version": 2,
        "creator_id": "creator",
        "nodes": [
            _snapshot_node("creator", None, provenance=provenance),
            _snapshot_node("recorded-child", "creator"),
        ],
    }
    with pytest.raises(BadRequest, match="budget"):
        preflight_topology(daemon, checkpoint_id=9, snapshot=snapshot, caller_id="caller")


async def test_lost_progress_claim_halts_and_returns_partial(
    client, daemon, fake_tmux, monkeypatch
) -> None:
    _participant(daemon, "creator", pane="%1")
    _participant(daemon, "restorer", pane="%2")
    fake_tmux.add_pane("%1", command="vibe")
    fake_tmux.add_pane("%2", command="vibe")
    created = await client.call("checkpoint.create", caller_id="creator", name="lost-claim")

    monkeypatch.setattr(daemon.store, "persist_restore_progress", lambda *a, **kw: False)
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer",
    )

    assert result["restore_state"] == "partial"
    assert result["creator"]["action"] == "reused_live"
    assert any("lost restore claim" in warning for warning in result["warnings"])
    row = daemon.store.get_checkpoint(created["checkpoint_id"])
    assert row is not None and row["restore_state"] == "partial"


async def test_harness_mismatch_is_failed_without_touching_pane(client, daemon, fake_tmux) -> None:
    """A harness mismatch on the creator's pane is refused by preflight.

    Previously this consumed the checkpoint (terminal ``failed``).  Now
    preflight catches it before the claim, so the checkpoint stays
    ``ready`` and retryable — the old test asserted the consumed-claim
    behaviour, which was the bug.
    """
    _participant(daemon, "creator", pane="%1")
    _participant(daemon, "restorer", pane="%2")
    fake_tmux.add_pane("%1", command="claude")
    fake_tmux.add_pane("%2", command="vibe")
    created = await client.call("checkpoint.create", caller_id="creator", name="mismatch")

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id="restorer",
        )
    assert exc.value.code == "bad_request"
    assert "runs" in str(exc.value)
    # The checkpoint must NOT have been consumed.
    cp = daemon.store.get_checkpoint(created["checkpoint_id"])
    assert cp is not None and cp["restore_state"] == "ready"
    # No pane was touched.
    assert fake_tmux.sent == []


async def test_completed_parent_becomes_promptless_lineage_anchor(
    client, daemon, fake_tmux, monkeypatch
) -> None:
    provenance = {
        "prompt": "original completed work",
        "cwd_requested": "/tmp",
        "cwd_resolved": "/tmp",
        "response_format": {"type": "object"},
    }
    _participant(daemon, "creator", pane="%1")
    _participant(daemon, "middle", parent_id="creator", dead=True, provenance=provenance)
    _participant(daemon, "grandchild", parent_id="middle", dead=True, provenance=provenance)
    _participant(daemon, "restorer", pane="%2")
    fake_tmux.add_pane("%1", command="vibe")
    fake_tmux.add_pane("%2", command="vibe")
    _job(
        daemon,
        "middle-spawn",
        caller_id="creator",
        target_id="middle",
        state="done",
        prompt="completed prompt",
    )
    _job(
        daemon,
        "grand-spawn",
        caller_id="middle",
        target_id="grandchild",
        state="running",
        prompt="unfinished prompt",
    )
    created = await client.call("checkpoint.create", caller_id="creator", name="anchor")

    calls: list[dict] = []

    async def fake_spawn(_daemon, params):
        calls.append(dict(params))
        new_id = f"restored-{len(calls)}"
        participant = Participant(
            id=new_id,
            harness=params["harness"],
            tier=Tier.SPAWNED,
            cwd=params["cwd"],
            parent_id=params["parent_id"],
            tmux_pane=f"%new-{len(calls)}",
        )
        daemon.store.upsert_participant(participant)
        return participant.to_dict()

    import theater.daemon.methods as methods_mod

    monkeypatch.setattr(methods_mod, "_spawn", fake_spawn)
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer",
    )

    middle = next(p for p in result["participants"] if p["original_participant_id"] == "middle")
    grandchild = next(
        p for p in result["participants"] if p["original_participant_id"] == "grandchild"
    )
    assert middle["action"] == "respawned"
    assert calls[0]["prompt"] is None
    assert "response_format" not in calls[0]
    assert calls[1]["prompt"] == "unfinished prompt"
    assert grandchild["new_parent_id"] == middle["new_participant_id"]


async def test_response_format_dict_round_trips_on_cold_respawn(daemon, monkeypatch) -> None:
    captured: dict = {}

    async def fake_spawn(_daemon, params):
        captured.update(params)
        participant = Participant(
            id="new-node",
            harness="vibe",
            tier=Tier.SPAWNED,
            cwd="/tmp",
            parent_id="parent",
            tmux_pane="%new",
        )
        daemon.store.upsert_participant(participant)
        return participant.to_dict()

    import theater.daemon.methods as methods_mod

    monkeypatch.setattr(methods_mod, "_spawn", fake_spawn)
    schema = {"type": "object", "required": ["answer"]}
    recorded = _snapshot_node(
        "old-node",
        "parent",
        provenance={
            "prompt": "answer",
            "cwd_requested": "/tmp",
            "cwd_resolved": "/tmp",
            "response_format": schema,
        },
    )
    _participant(daemon, "parent")
    participant, action, _warnings = await _spawn_node(
        daemon=daemon,
        orig_id="old-node",
        recorded=recorded,
        live_participant=None,
        harness="vibe",
        new_parent_id="parent",
        approval="yolo",
        live_jobs_by_handle={},
    )
    assert action == "respawned"
    assert participant.id == "new-node"
    assert captured["response_format"] == schema


async def test_v2_report_has_exact_identity_and_summary_fields(client, daemon, fake_tmux) -> None:
    _participant(daemon, "creator", pane="%1")
    _participant(daemon, "restorer", pane="%2")
    fake_tmux.add_pane("%1", command="vibe")
    fake_tmux.add_pane("%2", command="vibe")
    created = await client.call("checkpoint.create", caller_id="creator", name="contract")
    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer",
    )

    assert {"summary", "warnings", "jobs", "participants"} <= result.keys()
    report = result["creator"]
    assert {
        "original_participant_id",
        "current_participant_id",
        "new_participant_id",
        "original_parent_id",
        "current_parent_id",
        "new_parent_id",
        "harness",
        "original_session_id",
        "current_session_id",
        "action",
        "status",
        "reason",
    } <= report.keys()
    assert report["action"] in {"reused_live", "resumed", "respawned", "skipped", "failed"}
