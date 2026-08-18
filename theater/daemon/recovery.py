"""Orchestration-tree checkpoint recovery.

This module owns the recursive snapshot and multi-node restore logic for
checkpoint v2. Checkpoint v1 (creator-only jobs snapshot) remains readable and
is handled by a compatibility shim.

Snapshot format v2
------------------
The ``jobs_snapshot`` column stores a JSON object::

    {
        "version": 2,
        "creator_id": "<participant_id>",
        "nodes": [
            {
                "participant_id": str,
                "harness": str,
                "tier": str,
                "status": str,
                "parent_id": str | null,
                "session_id": str | null,
                "session_correlation": str | null,
                "cwd": str | null,
                "branch": str | null,
                "launch_provenance": dict | null,  -- parsed from JSON blob
                "jobs": [                           -- jobs this participant sent
                    {"handle": ..., "target_id": ..., "kind": ..., ...}
                ]
            },
            ...
        ]
    }

Version 1 (legacy) snapshots store a flat list of job dicts for the creator
only. They are detected by the absence of a ``"version"`` key (or a value < 2)
and treated as creator-only with no descendant information.

Classification
--------------
Each recorded node is classified at restore time as one of:

- ``live``:         participant exists and is not DEAD → reused in place
- ``resumable``:    DEAD with a trusted session_id → resume native session
- ``respawnable``:  DEAD with launch_provenance but no trusted session → cold respawn
- ``completed``:    DEAD with no provenance and no open jobs → skip (work is done)
- ``pruned``:       participant row has been GC'd entirely → skip or fail based on open jobs
- ``failed``:       cannot be restored (EXTERNAL, no cwd, etc.)

Action codes
------------
- ``reused_live``:  live node reused without spawning
- ``resumed``:      dead node resumed via native session id
- ``respawned``:    dead node cold-respawned from launch provenance
- ``skipped``:      completed/pruned node with no open work; nothing to do
- ``failed``:       could not restore; error recorded in report

Rail enforcement
----------------
Depth, budget, model, and reasoning rails are applied on every cold respawn.
Resume paths do not re-apply model/reasoning rails (the original spawn already
passed them), but depth and budget are always checked because the new parent
may be a different node at a different depth.

Parent-child-grandchild lineage
---------------------------------
The restorer acts as the caller for the restored creator. Recorded children of
the creator become children of the newly restored creator, and so on recursively.
Live nodes that already have a parent are left alone; dead nodes that are
re-spawned get their parent_id set to the restored parent.

Sends vs spawns
---------------
Send jobs are reported in the per-node job reconciliation but are never
replayed. Only spawn jobs for incomplete work drive new spawns. A spawn job is
"incomplete" when the target is dead/pruned and was not itself classified as
completed.

Partial failures
----------------
If one sibling fails to restore, siblings that already restored successfully
are left alive. The restore report records both the successes and the failure.
The checkpoint is finalized as ``restored`` when the creator is restored (even
if some descendants failed); their individual ``action=failed`` entries appear
in the report.

Single-use claim
----------------
The caller acquires the atomic claim before calling ``restore_tree``. This
module does not claim; it only finalizes via the caller's token.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from theater.daemon import lineage
from theater.daemon.rails import (
    check_budget,
    check_depth,
    check_model_allowed,
    check_reasoning_allowed,
)
from theater.models import BadRequest, Participant, Status, Tier
from theater.provenance import is_trusted_provenance

if TYPE_CHECKING:
    from theater.daemon.server import Daemon

logger = logging.getLogger("theater.recovery")

# ---- snapshot construction -----------------------------------------------

#: Keys captured from jobs rows for the v2 snapshot.
#: Intentionally matches _CHECKPOINT_JOB_KEYS in methods.py for backward compat —
#: the flat ``recorded_jobs`` in checkpoint.read returns jobs with these keys only.
_SNAPSHOT_JOB_KEYS = (
    "handle",
    "target_id",
    "kind",
    "prompt",
    "state",
    "result",
    "error_code",
    "created_at",
    "finished_at",
)


def build_tree_snapshot(daemon: Daemon, creator_id: str) -> dict:
    """Build a v2 tree snapshot for *creator_id* and all its descendants.

    Walks the lineage tree breadth-first starting at *creator_id*. For each
    node, records the participant's identity fields (from the live registry)
    and all jobs that participant **sent** (``caller_id == participant_id``).

    Returns a dict ready for ``json.dumps`` — stored as the ``jobs_snapshot``
    column value.
    """
    from sqlalchemy import select

    from theater.daemon.schema import jobs as jobs_table

    store = daemon.store

    # Breadth-first traversal of the orchestration tree rooted at creator_id.
    all_ids = lineage.subtree_ids(store, creator_id)

    nodes: list[dict] = []
    for pid in all_ids:
        p = store.get_participant(pid)
        if p is None:
            # Participant was GC'd between snapshot start and this lookup.
            # Record a stub so restore knows this id existed.
            nodes.append(
                {
                    "participant_id": pid,
                    "harness": None,
                    "tier": None,
                    "status": "dead",
                    "parent_id": None,
                    "session_id": None,
                    "session_correlation": None,
                    "cwd": None,
                    "branch": None,
                    "launch_provenance": None,
                    "jobs": [],
                }
            )
            continue

        # Fetch all jobs this participant sent.
        rows = store.conn.execute(
            select(*(jobs_table.c[k] for k in _SNAPSHOT_JOB_KEYS))
            .where(jobs_table.c.caller_id == pid)
            .order_by(jobs_table.c.created_at.asc(), jobs_table.c.handle.asc())
        ).fetchall()
        participant_jobs = [{k: r._mapping[k] for k in _SNAPSHOT_JOB_KEYS} for r in rows]

        # Parse launch_provenance blob if present.
        provenance_dict: dict | None = None
        if p.launch_provenance:
            with contextlib.suppress(ValueError, TypeError):
                provenance_dict = json.loads(p.launch_provenance)

        nodes.append(
            {
                "participant_id": p.id,
                "harness": p.harness,
                "tier": str(p.tier),
                "status": str(p.status),
                "parent_id": p.parent_id,
                "session_id": p.session_id,
                "session_correlation": p.session_correlation,
                "cwd": p.cwd,
                "branch": p.branch,
                "launch_provenance": provenance_dict,
                "jobs": participant_jobs,
            }
        )

    return {
        "version": 2,
        "creator_id": creator_id,
        "nodes": nodes,
    }


def is_v2_snapshot(snapshot_data: Any) -> bool:
    """Return True iff *snapshot_data* is a parsed v2 snapshot dict."""
    return isinstance(snapshot_data, dict) and snapshot_data.get("version") == 2


def parse_snapshot(raw: str) -> Any:
    """Parse the raw JSON string from ``jobs_snapshot``.

    Returns a list (v1) or dict (v2). Raises ``ValueError`` on bad JSON.
    """
    return json.loads(raw or "[]")


# ---- restore report -------------------------------------------------------


@dataclass
class NodeRestoreReport:
    """Per-participant restore outcome, included in the final restore report."""

    original_participant_id: str
    new_participant_id: str | None  # None when action is skipped/failed
    original_parent_id: str | None
    current_parent_id: str | None  # live parent at restore time
    new_parent_id: str | None  # parent of the newly spawned participant
    harness: str | None
    old_session_id: str | None
    new_session_id: str | None
    classification: str  # live | resumable | respawnable | completed | pruned | failed
    action: str  # reused_live | resumed | respawned | skipped | failed
    final_status: str | None
    reason: str
    jobs: list[dict] = field(default_factory=list)  # jobs sent by this node


@dataclass
class TreeRestoreResult:
    """Aggregate result of a full orchestration-tree restore."""

    checkpoint_id: int
    creator_report: NodeRestoreReport
    descendant_reports: list[NodeRestoreReport]
    partial_failures: list[str]  # participant_ids that failed

    def to_dict(self) -> dict:
        return {
            "checkpoint_id": self.checkpoint_id,
            "creator": _node_report_to_dict(self.creator_report),
            "descendants": [_node_report_to_dict(r) for r in self.descendant_reports],
            "partial_failures": self.partial_failures,
        }


def _node_report_to_dict(r: NodeRestoreReport) -> dict:
    return {
        "original_participant_id": r.original_participant_id,
        "new_participant_id": r.new_participant_id,
        "original_parent_id": r.original_parent_id,
        "current_parent_id": r.current_parent_id,
        "new_parent_id": r.new_parent_id,
        "harness": r.harness,
        "old_session_id": r.old_session_id,
        "new_session_id": r.new_session_id,
        "classification": r.classification,
        "action": r.action,
        "final_status": r.final_status,
        "reason": r.reason,
        "jobs": r.jobs,
    }


# ---- classification -------------------------------------------------------


def classify_node(
    recorded: dict,
    live_participant: Participant | None,
) -> tuple[str, str]:
    """Return (classification, reason) for a recorded snapshot node.

    ``live_participant`` is the current DB row for the participant (None if GC'd).

    Classifications:
    - ``live``:         live participant with pane
    - ``resumable``:    dead, has trusted session_id
    - ``respawnable``:  dead or pruned, has launch_provenance with usable cwd
    - ``completed``:    dead or pruned, no open work, no provenance → skip
    - ``pruned``:       GC'd from DB, not respawnable
    - ``failed``:       EXTERNAL tier, or otherwise unrestorable
    """
    orig_id = recorded["participant_id"]

    if live_participant is not None:
        if live_participant.status is not Status.DEAD:
            # Participant is alive — reuse in place.
            tier = live_participant.tier
            if tier is Tier.EXTERNAL:
                return "failed", f"participant {orig_id!r} is live but EXTERNAL (no pane)"
            return "live", "participant is live and addressable"

        # Participant row exists but is DEAD.
        session_id = live_participant.session_id
        session_corr = live_participant.session_correlation
        if session_id and is_trusted_provenance(session_corr):
            return "resumable", f"dead with trusted session_id {session_id!r}"

        # Check if we have usable provenance for a cold respawn.
        if live_participant.launch_provenance:
            try:
                prov = json.loads(live_participant.launch_provenance)
                if prov.get("cwd_resolved") or prov.get("cwd_requested"):
                    return "respawnable", "dead with launch_provenance for cold respawn"
            except (ValueError, TypeError):
                pass

        # Dead with no session and no usable provenance. Check open jobs.
        open_jobs = [j for j in recorded.get("jobs", []) if j.get("state") == "running"]
        if open_jobs:
            return "failed", (
                f"participant {orig_id!r} is dead with {len(open_jobs)} open job(s) "
                f"but no session_id or launch_provenance for respawn"
            )
        return "completed", "dead with no open work and no provenance; nothing to restore"

    # Participant row is GC'd — fall back to snapshot provenance.
    prov = recorded.get("launch_provenance")
    session_id = recorded.get("session_id")
    session_corr = recorded.get("session_correlation")

    if session_id and is_trusted_provenance(session_corr):
        return "resumable", f"pruned from DB but snapshot has trusted session_id {session_id!r}"

    if isinstance(prov, dict) and (prov.get("cwd_resolved") or prov.get("cwd_requested")):
        return "respawnable", "pruned from DB but snapshot has launch_provenance for cold respawn"

    open_jobs = [j for j in recorded.get("jobs", []) if j.get("state") == "running"]
    if open_jobs:
        return "pruned", (
            f"participant {orig_id!r} was GC'd with {len(open_jobs)} open job(s); "
            f"cannot restore without provenance"
        )
    return "completed", "GC'd with no open work; nothing to restore"


# ---- restore orchestration -----------------------------------------------


async def restore_tree(
    daemon: Daemon,
    *,
    checkpoint_id: int,
    snapshot: dict,
    caller_id: str,
    approval: str,
) -> TreeRestoreResult:
    """Restore the orchestration tree from a v2 snapshot.

    *caller_id* is the participant calling restore; it becomes the effective
    parent of the restored creator. The snapshot must be a parsed v2 dict.

    This function does NOT claim or finalize the checkpoint — the caller in
    ``methods.py`` owns the atomic claim. It does NOT call ``_spawn`` directly;
    instead it calls ``_do_spawn`` which goes through the same flow as the
    standard spawn method, so all rails (depth, budget, model, reasoning) apply.
    """
    creator_id = snapshot["creator_id"]
    nodes_by_id: dict[str, dict] = {n["participant_id"]: n for n in snapshot["nodes"]}

    # Map original_id → new_participant_id (for live nodes: same id)
    id_map: dict[str, str] = {}
    reports: list[NodeRestoreReport] = []

    # Build ordered restore sequence: creator first, then children, BFS.
    restore_order = _bfs_order(creator_id, nodes_by_id)

    creator_report: NodeRestoreReport | None = None

    for orig_id in restore_order:
        recorded = nodes_by_id.get(orig_id)
        if recorded is None:
            # Referenced in lineage but not captured — treat as pruned.
            recorded = {
                "participant_id": orig_id,
                "harness": None,
                "tier": None,
                "status": "dead",
                "parent_id": None,
                "session_id": None,
                "session_correlation": None,
                "cwd": None,
                "branch": None,
                "launch_provenance": None,
                "jobs": [],
            }

        live_participant = daemon.store.get_participant(orig_id)
        classification, reason = classify_node(recorded, live_participant)

        # Determine the effective new parent id for this node.
        original_parent_id = recorded.get("parent_id")
        if orig_id == creator_id:
            # Creator's parent in the restored tree is the caller.
            new_parent_id = caller_id
        else:
            # For descendants: use the already-restored parent id from the map,
            # or the original parent if it wasn't in the snapshot (shouldn't happen).
            new_parent_id = id_map.get(original_parent_id or "", original_parent_id or "")

        # Current live state of the recorded parent (for reporting).
        current_parent_id: str | None = None
        if live_participant is not None:
            current_parent_id = live_participant.parent_id

        if orig_id == creator_id:
            # Creator restoration errors propagate — the caller expects a failure
            # to be signalled so the checkpoint can be marked 'failed' in the DB.
            report = await _restore_node(
                daemon=daemon,
                orig_id=orig_id,
                recorded=recorded,
                live_participant=live_participant,
                classification=classification,
                reason=reason,
                new_parent_id=new_parent_id,
                original_parent_id=original_parent_id,
                current_parent_id=current_parent_id,
                approval=approval,
                caller_id=caller_id,
            )
            if report.action == "failed":
                raise BadRequest(
                    f"checkpoint {checkpoint_id!r}: creator restoration failed: {report.reason}"
                )
        else:
            # Descendant restoration errors are caught and reported, never raised.
            report = await _restore_node(
                daemon=daemon,
                orig_id=orig_id,
                recorded=recorded,
                live_participant=live_participant,
                classification=classification,
                reason=reason,
                new_parent_id=new_parent_id,
                original_parent_id=original_parent_id,
                current_parent_id=current_parent_id,
                approval=approval,
                caller_id=caller_id,
            )

        # Update the id map so descendants know the new id.
        if report.new_participant_id is not None:
            id_map[orig_id] = report.new_participant_id
        else:
            id_map[orig_id] = orig_id  # skipped/failed: keep original for lineage tracking

        reports.append(report)
        if orig_id == creator_id:
            creator_report = report

    assert creator_report is not None, "creator node was not in restore_order"

    descendant_reports = [r for r in reports if r.original_participant_id != creator_id]
    partial_failures = [
        r.original_participant_id for r in descendant_reports if r.action == "failed"
    ]

    return TreeRestoreResult(
        checkpoint_id=checkpoint_id,
        creator_report=creator_report,
        descendant_reports=descendant_reports,
        partial_failures=partial_failures,
    )


def _bfs_order(creator_id: str, nodes_by_id: dict[str, dict]) -> list[str]:
    """Return node ids in BFS order starting from creator_id.

    Children are determined from the ``parent_id`` field in the snapshot.
    Any nodes whose parent is not in the snapshot are attached to the creator.
    """
    # Build parent→children mapping from snapshot.
    children_of: dict[str, list[str]] = {nid: [] for nid in nodes_by_id}
    for nid, node in nodes_by_id.items():
        parent = node.get("parent_id")
        if parent and parent in children_of:
            children_of[parent].append(nid)
        elif nid != creator_id and creator_id in children_of:
            # Orphan relative to snapshot — treat as child of creator.
            children_of[creator_id].append(nid)

    order: list[str] = []
    queue = [creator_id]
    seen: set[str] = set()
    while queue:
        nid = queue.pop(0)
        if nid in seen:
            continue
        seen.add(nid)
        order.append(nid)
        queue.extend(children_of.get(nid, []))
    return order


async def _restore_node(
    *,
    daemon: Daemon,
    orig_id: str,
    recorded: dict,
    live_participant: Participant | None,
    classification: str,
    reason: str,
    new_parent_id: str | None,
    original_parent_id: str | None,
    current_parent_id: str | None,
    approval: str,
    caller_id: str,
) -> NodeRestoreReport:
    """Restore a single node and return its report."""
    jobs = recorded.get("jobs", [])
    harness = recorded.get("harness") or (live_participant.harness if live_participant else None)
    old_session_id = recorded.get("session_id") or (
        live_participant.session_id if live_participant else None
    )

    if classification == "live":
        assert live_participant is not None
        # Live node: reuse in place without spawning. Do not reparent.
        return NodeRestoreReport(
            original_participant_id=orig_id,
            new_participant_id=orig_id,
            original_parent_id=original_parent_id,
            current_parent_id=current_parent_id,
            new_parent_id=current_parent_id,  # unchanged
            harness=harness,
            old_session_id=old_session_id,
            new_session_id=live_participant.session_id,
            classification=classification,
            action="reused_live",
            final_status=str(live_participant.status),
            reason=reason,
            jobs=jobs,
        )

    if classification in ("completed", "pruned") and _has_no_open_work(recorded):
        return NodeRestoreReport(
            original_participant_id=orig_id,
            new_participant_id=None,
            original_parent_id=original_parent_id,
            current_parent_id=current_parent_id,
            new_parent_id=None,
            harness=harness,
            old_session_id=old_session_id,
            new_session_id=None,
            classification=classification,
            action="skipped",
            final_status="dead",
            reason=reason,
            jobs=jobs,
        )

    if classification == "pruned" and not _has_no_open_work(recorded):
        # Pruned with open work — we already reported this as pruned/failed in classify.
        return NodeRestoreReport(
            original_participant_id=orig_id,
            new_participant_id=None,
            original_parent_id=original_parent_id,
            current_parent_id=current_parent_id,
            new_parent_id=None,
            harness=harness,
            old_session_id=old_session_id,
            new_session_id=None,
            classification=classification,
            action="failed",
            final_status="dead",
            reason=reason,
            jobs=jobs,
        )

    if classification == "failed":
        return NodeRestoreReport(
            original_participant_id=orig_id,
            new_participant_id=None,
            original_parent_id=original_parent_id,
            current_parent_id=current_parent_id,
            new_parent_id=None,
            harness=harness,
            old_session_id=old_session_id,
            new_session_id=None,
            classification=classification,
            action="failed",
            final_status=str(live_participant.status) if live_participant else "dead",
            reason=reason,
            jobs=jobs,
        )

    # resumable or respawnable — need to spawn.
    if not harness:
        return NodeRestoreReport(
            original_participant_id=orig_id,
            new_participant_id=None,
            original_parent_id=original_parent_id,
            current_parent_id=current_parent_id,
            new_parent_id=None,
            harness=harness,
            old_session_id=old_session_id,
            new_session_id=None,
            classification=classification,
            action="failed",
            final_status="dead",
            reason=f"cannot spawn: harness is unknown for participant {orig_id!r}",
            jobs=jobs,
        )

    try:
        new_participant, action = await _spawn_node(
            daemon=daemon,
            orig_id=orig_id,
            recorded=recorded,
            live_participant=live_participant,
            classification=classification,
            harness=harness,
            new_parent_id=new_parent_id,
            approval=approval,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("failed to restore node %s: %s", orig_id, exc, exc_info=True)
        return NodeRestoreReport(
            original_participant_id=orig_id,
            new_participant_id=None,
            original_parent_id=original_parent_id,
            current_parent_id=current_parent_id,
            new_parent_id=new_parent_id,
            harness=harness,
            old_session_id=old_session_id,
            new_session_id=None,
            classification=classification,
            action="failed",
            final_status="dead",
            reason=str(exc),
            jobs=jobs,
        )

    return NodeRestoreReport(
        original_participant_id=orig_id,
        new_participant_id=new_participant.id,
        original_parent_id=original_parent_id,
        current_parent_id=current_parent_id,
        new_parent_id=new_parent_id,
        harness=harness,
        old_session_id=old_session_id,
        new_session_id=new_participant.session_id,
        classification=classification,
        action=action,
        final_status=str(new_participant.status),
        reason=reason,
        jobs=jobs,
    )


async def _spawn_node(
    *,
    daemon: Daemon,
    orig_id: str,
    recorded: dict,
    live_participant: Participant | None,
    classification: str,
    harness: str,
    new_parent_id: str | None,
    approval: str,
) -> tuple[Participant, str]:
    """Spawn (resume or cold-respawn) a node and return (participant, action).

    Applies depth, budget, model, and reasoning rails. Resume paths skip
    model/reasoning rails (already applied at original spawn time), but still
    enforce depth and budget.
    """
    import theater.daemon.methods as _methods_mod

    _methods_spawn = _methods_mod._spawn

    rails = daemon.config.rails

    # Get provenance from the live row if present, else from snapshot.
    prov: dict = {}
    if live_participant is not None and live_participant.launch_provenance:
        with contextlib.suppress(ValueError, TypeError):
            prov = json.loads(live_participant.launch_provenance)
    if not prov and isinstance(recorded.get("launch_provenance"), dict):
        prov = recorded["launch_provenance"]

    # Resolve cwd: prefer resolved, fall back to requested.
    cwd = prov.get("cwd_resolved") or prov.get("cwd_requested") or recorded.get("cwd")
    if not cwd:
        raise BadRequest(
            f"cannot restore participant {orig_id!r}: no cwd available in provenance or snapshot"
        )

    # Rail checks (depth + budget always; model/reasoning only for cold respawn).
    check_depth(daemon.store, new_parent_id, cap=rails.depth_cap)
    check_budget(daemon.store, new_parent_id, limit=rails.budget)

    if classification == "resumable":
        # Resume the native session. Use session_id from live row first, snapshot second.
        session_id = (live_participant.session_id if live_participant else None) or recorded.get(
            "session_id"
        )
        params: dict = {
            "harness": harness,
            "cwd": cwd,
            "approval": approval,
            "parent_id": new_parent_id,
            "resume": session_id,
        }
        # Prompt and response_format cannot be passed to resume-only harnesses;
        # _spawn will raise if the harness does not support prompt-at-resume.
        # We send an empty prompt so the harness starts the session view.
        result = await _methods_spawn(daemon, params)
        p = daemon.store.get_participant(result["id"])
        assert p is not None
        return p, "resumed"

    # Cold respawn from provenance.
    model = prov.get("model")
    reasoning_effort = prov.get("reasoning_effort")
    # We do NOT restore worktrees during cold respawn — the worktree directory
    # may no longer exist and we have no way to verify it. The child will run
    # in the cwd_resolved directory directly (which may be the worktree path).
    # response_format is not included in a cold respawn prompt — sends handle that.

    # Apply model/reasoning rails for cold respawn.
    check_model_allowed(harness, model, daemon.config.models_for(harness))
    check_reasoning_allowed(harness, reasoning_effort, daemon.config.reasoning_for(harness))

    params = {
        "harness": harness,
        "cwd": cwd,
        "approval": approval,
        "parent_id": new_parent_id,
        "prompt": "",  # No prompt: the restorer sends recovery instructions via send
    }
    if model:
        params["model"] = model
    if reasoning_effort:
        params["reasoning_effort"] = reasoning_effort

    result = await _methods_spawn(daemon, params)
    p = daemon.store.get_participant(result["id"])
    assert p is not None
    return p, "respawned"


def _has_no_open_work(recorded: dict) -> bool:
    """Return True iff the recorded node has no running jobs."""
    return not any(j.get("state") == "running" for j in recorded.get("jobs", []))


# ---- v1 compatibility ---------------------------------------------------


def upgrade_v1_snapshot_for_read(snapshot_data: Any, creator_id: str) -> dict:
    """Wrap a v1 jobs list into a v2-shaped dict for uniform handling.

    Used only for reading/reporting — never written back to the database.
    The returned dict has ``version=1`` (not 2) to signal degraded mode.
    """
    if isinstance(snapshot_data, list):
        # Classic v1: flat list of job dicts for the creator only.
        return {
            "version": 1,
            "creator_id": creator_id,
            "nodes": [
                {
                    "participant_id": creator_id,
                    "harness": None,
                    "tier": None,
                    "status": "dead",
                    "parent_id": None,
                    "session_id": None,
                    "session_correlation": None,
                    "cwd": None,
                    "branch": None,
                    "launch_provenance": None,
                    "jobs": snapshot_data,
                }
            ],
        }
    return snapshot_data


# ---- restore entry point (called from methods.py) -----------------------


async def restore_checkpoint(
    daemon: Daemon,
    *,
    checkpoint_id: int,
    row: dict,
    caller_id: str,
    approval: str,
) -> dict:
    """Entry point called from ``checkpoint.restore`` after the claim is acquired.

    Handles both v1 (legacy, creator-only) and v2 (full-tree) snapshots.
    Returns the result dict to be serialized into ``restore_result``.

    For v1 snapshots: falls back to the original single-node restore logic
    (creator only, no descendants), returning a legacy-shaped result dict.

    For v2 snapshots: orchestrates the full tree restore and returns the
    structured ``TreeRestoreResult`` dict.

    Raises any exception that should mark the checkpoint as ``failed``.
    """
    raw = row.get("jobs_snapshot") or "[]"
    try:
        snapshot_data = parse_snapshot(raw)
    except (ValueError, TypeError) as exc:
        raise BadRequest(f"checkpoint {checkpoint_id!r} has malformed snapshot: {exc}") from exc

    if is_v2_snapshot(snapshot_data):
        result = await restore_tree(
            daemon,
            checkpoint_id=checkpoint_id,
            snapshot=snapshot_data,
            caller_id=caller_id,
            approval=approval,
        )
        return result.to_dict()

    # v1 fallback: restore creator only (original behaviour).
    return await _restore_v1(
        daemon=daemon,
        checkpoint_id=checkpoint_id,
        row=row,
        caller_id=caller_id,
        approval=approval,
        snapshot_data=snapshot_data,
    )


async def _restore_v1(
    daemon: Daemon,
    *,
    checkpoint_id: int,
    row: dict,
    caller_id: str,
    approval: str,
    snapshot_data: Any,
) -> dict:
    """Restore a v1 (creator-only) checkpoint using the original single-node logic.

    Returns a dict shaped identically to the v1 restore result so existing
    callers (MCP tools, tests) continue to work unchanged.
    """
    import theater.daemon.methods as _methods_mod

    _methods_spawn = _methods_mod._spawn
    from theater.models import Status

    parent_id = row["participant_id"]
    parent = daemon.store.get_participant(parent_id)

    # Validate the parent (may have been externally modified since we last checked).
    if parent is None:
        raise BadRequest(
            f"checkpoint {checkpoint_id!r}: creator participant {parent_id!r} has been "
            f"pruned from retention and cannot be restored"
        )
    if str(parent.tier) == str(Tier.EXTERNAL):
        raise BadRequest(
            f"checkpoint {checkpoint_id!r}: creator participant {parent_id!r} is EXTERNAL"
        )

    parent_is_live = parent.status is not Status.DEAD

    if parent_is_live:
        action = "live"
        restored = parent.to_dict()
    elif parent.session_id is not None:
        restored = await _methods_spawn(
            daemon,
            {
                "harness": parent.harness,
                "cwd": parent.cwd,
                "approval": approval,
                "parent_id": caller_id,
                "resume": parent.session_id,
            },
        )
        action = "resumed"
    else:
        restored = await _methods_spawn(
            daemon,
            {
                "harness": parent.harness,
                "cwd": parent.cwd,
                "approval": approval,
                "parent_id": caller_id,
            },
        )
        action = "respawned"

    recorded_jobs = snapshot_data if isinstance(snapshot_data, list) else []

    return {
        "checkpoint_id": checkpoint_id,
        "restored_parent": {
            "participant_id": restored["id"],
            "harness": restored["harness"],
            "status": restored["status"],
            "session_id": restored.get("session_id"),
            "action": action,
            "handoff_required": True,
        },
        "recorded_jobs": recorded_jobs,
        # Marker so callers can tell this was a v1 restore.
        "_snapshot_version": 1,
    }
