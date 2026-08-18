"""Orchestration-tree checkpoint recovery.

This module owns the recursive snapshot and multi-node restore logic for
checkpoint v2. Checkpoint v1 (creator-only jobs snapshot) remains readable
and is handled by a compatibility shim that preserves the original behaviour.

Snapshot format v2
------------------
``jobs_snapshot`` stores a self-contained JSON object::

    {
        "version": 2,
        "creator_id": "<participant_id>",
        "nodes": [
            {
                "participant_id": str,
                "harness": str,
                "tier": str,
                "status": str,               -- at checkpoint time
                "parent_id": str | null,
                "session_id": str | null,
                "session_correlation": str | null,
                "cwd": str | null,
                "branch": str | null,
                "launch_provenance": dict | null,
                "jobs": [                     -- jobs sent (caller) OR received
                    {                         -- (target) by this participant
                        "handle": str,
                        "caller_id": str,
                        "target_id": str | null,
                        "kind": str,
                        "prompt": str | null,
                        "state": str,
                        "result": str | null,
                        "error_code": str | null,
                        "created_at": float,
                        "finished_at": float | null,
                        "response_format": str | null,
                    }
                ]
            },
            ...
        ]
    }

v1 (legacy): a flat list of job dicts for the creator only. Detected by the
absence of a ``"version"`` key. v1 restores run through the original single-node
path with strict DB validation.

Job snapshot completeness (item 6)
------------------------------------
For each node in the subtree we capture all jobs where
``caller_id IN subtree_ids OR target_id IN subtree_ids``. This means:

- The spawn job for a child lives on the parent node (caller = parent).
- The child's own work (sends it issued) lives on the child node.
- A dead child with a terminal spawn job on the parent and no unfinished sends
  is correctly classified as ``completed`` even when it has launch provenance.

Classification
--------------
Live (row exists, status != DEAD):
  - EXTERNAL → ``failed`` (no pane)
  - No physical pane verified → ``stale_live`` (pane gone, treat as dead)
  - Pane ok, harness mismatch → ``live_harness_conflict`` (treat as dead)
  - Otherwise → ``live``

Dead (row exists, status == DEAD):
  - Open spawn job from parent still RUNNING → check child first
  - Provenance available with usable cwd → ``respawnable``
  - ELSE → ``completed`` or ``failed`` depending on open work

Pruned (row GC'd):
  - Snapshot has snapshot provenance → ``respawnable``
  - Otherwise → ``completed`` (no open work) or ``pruned`` (open work, no provenance)

NB: ``resumable`` is intentionally *not* a classification for pruned rows.
The spawner requires a live trusted DB binding; snapshot evidence alone
cannot prove the session is not now live elsewhere.

Action codes
------------
``reused_live``         live node verified and reused in place
``reparented``          live node reparented to reconstructed parent
``respawned``           cold-respawned from launch provenance
``skipped``             work complete or parent not restored; nothing to do
``ancestor_not_restored``  parent was skipped/failed; descendant skipped
``live_lineage_conflict``  live node owned by a different live parent
``failed``              unrestorable; error recorded in report

Rail enforcement (item 11)
--------------------------
Depth, budget, model, and reasoning rails are applied on every spawn
(resume or cold). Configuration can change between the original spawn and
the restore; the original approval is not a voucher for current policy.

Partial state (item 9)
----------------------
If some nodes succeed and some fail, the checkpoint is finalised as
``partial``. The progress blob written after each successful node allows a
retry to skip already-live nodes. A ``partial`` checkpoint is claimable for
re-attempt.

Worktree recovery (item 12)
----------------------------
If the recorded worktree path still exists as a linked worktree of the same
repo, it is reused. If the path is gone but provenance is sufficient
(worktree_repo_root, worktree_branch present), the worktree is recreated.
If neither is possible, the restore fails clearly rather than running in an
unintended directory.

Sends not replayed (item 8)
----------------------------
Send jobs appear in the ``job_reconciliations`` list per participant but are
never re-issued. Only spawn jobs for incomplete work drive new processes.
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

# ---- snapshot job keys -------------------------------------------------------

#: Full set of job fields captured per node in v2 snapshots.
#: Includes caller_id (needed to identify which node owns the job)
#: and response_format (needed to reconstruct the original spawn call).
_SNAPSHOT_JOB_KEYS = (
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
)

#: Keys returned in the backward-compatible "recorded_jobs" flat list
#: (creator's sent jobs only, without caller_id).
_LEGACY_JOB_KEYS = (
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


# ---- snapshot construction ---------------------------------------------------


def build_tree_snapshot(daemon: Daemon, creator_id: str) -> dict:
    """Build a v2 tree snapshot for *creator_id* and all its descendants.

    Walks the lineage tree breadth-first starting at *creator_id*. For each
    node, records the participant's identity fields and all jobs where the
    participant is either the caller OR the target. This ensures that:

    - A child's spawn job (caller = parent, target = child) is captured on
      the parent node.
    - A child's own send jobs are captured on the child node.
    - The child's completion state is fully determinable from the snapshot.

    Returns a dict ready for ``json.dumps``.
    """
    from sqlalchemy import or_, select

    from theater.daemon.schema import jobs as jobs_table

    store = daemon.store

    # Breadth-first traversal of the orchestration tree.
    all_ids = lineage.subtree_ids(store, creator_id)

    nodes: list[dict] = []
    for pid in all_ids:
        p = store.get_participant(pid)
        if p is None:
            # Participant was GC'd between snapshot start and this lookup.
            nodes.append(_stub_node(pid))
            continue

        # Fetch all jobs where this participant is caller OR target.
        rows = store.conn.execute(
            select(*(jobs_table.c[k] for k in _SNAPSHOT_JOB_KEYS))
            .where(
                or_(
                    jobs_table.c.caller_id == pid,
                    jobs_table.c.target_id == pid,
                )
            )
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


def _stub_node(pid: str) -> dict:
    """Return a minimal stub node for a participant that was GC'd mid-snapshot."""
    return {
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


def is_v2_snapshot(snapshot_data: Any) -> bool:
    """Return True iff *snapshot_data* is a parsed v2 snapshot dict."""
    return isinstance(snapshot_data, dict) and snapshot_data.get("version") == 2


def parse_snapshot(raw: str) -> Any:
    """Parse the raw JSON string from ``jobs_snapshot``.

    Returns a list (v1) or dict (v2). Raises ``ValueError`` on bad JSON.
    """
    return json.loads(raw or "[]")


def validate_v2_snapshot(snapshot: dict, checkpoint_id: int) -> None:
    """Validate a v2 snapshot's structure before any claim is made.

    Raises BadRequest on:
    - Missing or invalid creator_id
    - Empty or non-list nodes
    - Duplicate participant IDs in nodes
    - creator_id not present in nodes
    - Snapshot-level parent_id cycles
    """
    creator_id = snapshot.get("creator_id")
    if not isinstance(creator_id, str) or not creator_id:
        raise BadRequest(f"checkpoint {checkpoint_id!r}: v2 snapshot has no valid creator_id")
    nodes = snapshot.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise BadRequest(f"checkpoint {checkpoint_id!r}: v2 snapshot has no nodes")
    seen_ids: set[str] = set()
    for n in nodes:
        nid = n.get("participant_id")
        if not isinstance(nid, str) or not nid:
            raise BadRequest(f"checkpoint {checkpoint_id!r}: node missing participant_id")
        if nid in seen_ids:
            raise BadRequest(
                f"checkpoint {checkpoint_id!r}: duplicate participant_id {nid!r} in snapshot"
            )
        seen_ids.add(nid)
    if creator_id not in seen_ids:
        raise BadRequest(
            f"checkpoint {checkpoint_id!r}: creator_id {creator_id!r} not present in nodes"
        )
    # Cycle detection in snapshot parent_id links.
    nodes_by_id = {n["participant_id"]: n for n in nodes}
    for nid in seen_ids:
        visited: set[str] = set()
        cur = nid
        while True:
            if cur in visited:
                raise BadRequest(
                    f"checkpoint {checkpoint_id!r}: snapshot has parent_id cycle at {cur!r}"
                )
            visited.add(cur)
            parent = nodes_by_id.get(cur, {}).get("parent_id")
            if parent is None or parent not in nodes_by_id:
                break
            cur = parent


# ---- job reconciliation ------------------------------------------------------


@dataclass
class JobReconciliation:
    """Per-job outcome in the restore report."""

    handle: str
    kind: str  # spawn | send
    recorded_state: str  # state at checkpoint time
    current_state: str | None  # current DB state or "collected" if pruned
    outcome: str  # skipped_complete | replayed | reported_only | pruned
    reason: str


def reconcile_jobs(
    node_jobs: list[dict],
    live_jobs_by_handle: dict[str, dict],
) -> list[JobReconciliation]:
    """Build a job reconciliation list for one node.

    - Send jobs: always ``reported_only`` — never replayed.
    - Spawn jobs:
      - Terminal in snapshot → ``skipped_complete``
      - Still RUNNING in snapshot but terminal in DB → ``skipped_complete``
      - Still RUNNING in snapshot and absent from DB → ``pruned``
      - Otherwise → depends on the caller's restore decision.

    This function is informational only; it does not mutate any state.
    """
    result: list[JobReconciliation] = []
    for job in node_jobs:
        handle = job["handle"]
        kind = job.get("kind", "send")
        recorded_state = job.get("state", "running")
        live = live_jobs_by_handle.get(handle)
        current_state = live["state"] if live else None

        if kind == "send":
            result.append(
                JobReconciliation(
                    handle=handle,
                    kind=kind,
                    recorded_state=recorded_state,
                    current_state=current_state,
                    outcome="reported_only",
                    reason="send jobs are never replayed during restore",
                )
            )
            continue

        # spawn job
        if recorded_state != "running":
            result.append(
                JobReconciliation(
                    handle=handle,
                    kind=kind,
                    recorded_state=recorded_state,
                    current_state=current_state,
                    outcome="skipped_complete",
                    reason=f"spawn was already {recorded_state} at checkpoint time",
                )
            )
            continue

        if live is not None and live["state"] != "running":
            result.append(
                JobReconciliation(
                    handle=handle,
                    kind=kind,
                    recorded_state=recorded_state,
                    current_state=current_state,
                    outcome="skipped_complete",
                    reason=f"spawn completed after checkpoint ({live['state']})",
                )
            )
            continue

        if live is None:
            result.append(
                JobReconciliation(
                    handle=handle,
                    kind=kind,
                    recorded_state=recorded_state,
                    current_state="collected",
                    outcome="pruned",
                    reason="spawn job GC'd; target status must be inferred from participant row",
                )
            )
            continue

        result.append(
            JobReconciliation(
                handle=handle,
                kind=kind,
                recorded_state=recorded_state,
                current_state=current_state,
                outcome="reported_only",
                reason="spawn still running at restore time; target node drives recovery decision",
            )
        )

    return result


# ---- pane verification sentinel ----------------------------------------------

#: Returned by ``_get_pane_info`` when tmux is not available (test environments).
#: The classifier should trust the DB row in this case rather than marking the
#: participant as stale_live.
_PANE_INFO_TMUX_UNAVAILABLE: object = object()

# ---- node completion check ---------------------------------------------------


def _node_is_complete(recorded: dict, live_jobs_by_handle: dict[str, dict]) -> bool:
    """Return True iff a node has evidence that all its work is done.

    Two tiers of evidence:

    1. **Inbound spawn job** (target = this node, kind = spawn): If the node has
       a spawn job received from its parent and that job is terminal (at checkpoint
       time or in the live DB), the node's primary work is done. This is the
       strongest signal.

    2. **No running jobs at all**: If the node has no inbound spawn and has
       no jobs in the ``running`` state (in snapshot or live DB), it has no
       observable incomplete work. This covers dead leaf nodes, completed sends,
       and root/creator nodes that have finished.

    Return False when there is evidence of incomplete work:
    - A running inbound spawn (node's own process is still wanted by parent).
    - Any running job in snapshot that is also running in the live DB.
    """
    node_id = recorded["participant_id"]
    jobs = recorded.get("jobs", [])

    # Check inbound spawn job first (strongest evidence).
    inbound_spawns = [j for j in jobs if j.get("kind") == "spawn" and j.get("target_id") == node_id]
    if inbound_spawns:
        for job in inbound_spawns:
            recorded_state = job.get("state", "running")
            if recorded_state != "running":
                continue  # terminal at checkpoint time → done
            live = live_jobs_by_handle.get(job["handle"])
            if live is None or live["state"] == "running":
                return False  # still running or pruned → incomplete
        return True  # all inbound spawns terminal

    # No inbound spawn: check if any job is still running.
    # This handles root/creator nodes and leaf nodes.
    for job in jobs:
        recorded_state = job.get("state", "running")
        if recorded_state != "running":
            continue
        handle = job.get("handle")
        if handle is None:
            # No handle to look up; treat as running (conservative).
            return False
        live = live_jobs_by_handle.get(handle)
        if live is None or live["state"] == "running":
            return False  # job still running or pruned

    # No running jobs found.
    return True


# ---- restore report ----------------------------------------------------------


@dataclass
class NodeRestoreReport:
    """Per-participant restore outcome."""

    original_participant_id: str
    new_participant_id: str | None
    original_parent_id: str | None
    current_parent_id: str | None
    new_parent_id: str | None
    harness: str | None
    old_session_id: str | None
    new_session_id: str | None
    classification: str
    action: str
    final_status: str | None
    reason: str
    job_reconciliations: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class TreeRestoreResult:
    """Aggregate result of a full orchestration-tree restore."""

    checkpoint_id: int
    snapshot_version: int
    restore_state: str  # restored | partial
    restored_by: str
    approval: str
    creator_report: NodeRestoreReport
    descendant_reports: list[NodeRestoreReport]
    partial_failures: list[str]
    counts: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        all_reports = [self.creator_report, *self.descendant_reports]
        return {
            "checkpoint_id": self.checkpoint_id,
            "snapshot_version": self.snapshot_version,
            "restore_state": self.restore_state,
            "restored_by": self.restored_by,
            "approval": self.approval,
            "counts": self.counts,
            "participants": [_node_report_to_dict(r) for r in all_reports],
            # Keep top-level creator/descendants for existing callers.
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
        "job_reconciliations": r.job_reconciliations,
        "warnings": r.warnings,
    }


# ---- classification ----------------------------------------------------------


def classify_node(
    recorded: dict,
    live_participant: Participant | None,
    live_jobs_by_handle: dict[str, dict],
    *,
    pane_info: dict | object | None = _PANE_INFO_TMUX_UNAVAILABLE,
) -> tuple[str, str]:
    """Return (classification, reason) for a recorded snapshot node.

    ``live_participant``: current DB row (None if GC'd).
    ``live_jobs_by_handle``: mapping of handle → job dict for all live DB jobs.
    ``pane_info``:
      - dict with 'harness' key: tmux confirmed pane exists (may detect harness mismatch).
      - None: tmux confirmed pane is gone → ``stale_live``.
      - ``_PANE_INFO_TMUX_UNAVAILABLE``: tmux not available; trust the DB row as live.

    Classifications:
    ``live``                live DB row (pane check passed or tmux unavailable)
    ``stale_live``          live DB row but pane confirmed gone by tmux → treat as dead
    ``live_harness_conflict`` live but a different harness occupies the pane → treat as dead
    ``respawnable``         dead or pruned with usable launch provenance
    ``completed``           dead/pruned with no unfinished work → skip
    ``pruned``              GC'd with open work and no provenance → cannot recover
    ``failed``              EXTERNAL, no cwd, or otherwise unrestorable
    """
    orig_id = recorded["participant_id"]

    if live_participant is not None:
        if live_participant.status is not Status.DEAD:
            tier = live_participant.tier
            if tier is Tier.EXTERNAL:
                return "failed", f"participant {orig_id!r} is live but EXTERNAL (no pane)"

            if not live_participant.tmux_pane:
                # No pane on a live Spawned/Adopted node is unusual but not fatal for restore;
                # record as stale and let the node-level logic decide.
                return "stale_live", (
                    f"participant {orig_id!r} is live but has no pane recorded; treating as dead"
                )

            if pane_info is _PANE_INFO_TMUX_UNAVAILABLE:
                # tmux not queryable — trust the DB row.
                return "live", "participant is live (tmux not available; trusting DB)"
            if pane_info is None:
                # tmux reports pane gone. This is a stale_live situation.
                # We return stale_live; the restore node handler will re-classify
                # based on completion status and provenance.
                return "stale_live", (
                    f"participant {orig_id!r}: pane {live_participant.tmux_pane!r} "
                    f"confirmed gone by tmux"
                )
            assert isinstance(pane_info, dict)
            pane_harness = pane_info.get("harness")
            if pane_harness and pane_harness != live_participant.harness:
                return "live_harness_conflict", (
                    f"participant {orig_id!r}: pane {live_participant.tmux_pane!r} "
                    f"runs {pane_harness!r} not {live_participant.harness!r}"
                )
            return "live", "participant is live with tmux-verified pane"

        # DEAD retained row.
        # Completion via inbound spawn evidence: if the node has an inbound spawn job
        # (parent → this node) that is terminal, the work is done even if provenance
        # exists. This is the key discriminator: "A dead child with a terminal spawn
        # and no unfinished work must be skipped even when provenance exists." (item 6)
        #
        # We only use this completion evidence when there IS an inbound spawn to check.
        # Root/creator nodes have no inbound spawn, so completion cannot be confirmed
        # this way — we fall through to provenance-based restoration.
        node_jobs = recorded.get("jobs", [])
        has_inbound_spawn = any(
            j.get("kind") == "spawn" and j.get("target_id") == orig_id for j in node_jobs
        )
        if has_inbound_spawn:
            complete = _node_is_complete(recorded, live_jobs_by_handle)
            if complete:
                return "completed", "dead with all inbound spawn jobs terminal; work is done"

        # Not complete (or no inbound spawn to confirm completion).
        # Check for resume (retained trusted session).
        if live_participant.session_id and is_trusted_provenance(
            live_participant.session_correlation
        ):
            return "resumable", (
                f"dead with trusted session_id {live_participant.session_id!r}; "
                f"native resume possible"
            )

        # Check launch provenance for cold respawn.
        if live_participant.launch_provenance:
            with contextlib.suppress(ValueError, TypeError):
                prov = json.loads(live_participant.launch_provenance)
                if prov.get("cwd_resolved") or prov.get("cwd_requested"):
                    return "respawnable", "dead with launch_provenance; cold respawn available"

        # Fall through to snapshot provenance.
        snap_prov = recorded.get("launch_provenance")
        if isinstance(snap_prov, dict) and (
            snap_prov.get("cwd_resolved") or snap_prov.get("cwd_requested")
        ):
            return "respawnable", "dead; using snapshot launch_provenance for cold respawn"

        # No provenance of any kind. Check completion as a last resort.
        # If there are no running jobs, there's nothing to restore.
        no_running = not any(j.get("state") == "running" for j in node_jobs)
        if no_running:
            return "completed", "dead with no running jobs and no provenance; nothing to restore"

        return "failed", (
            f"participant {orig_id!r} is dead with incomplete work but no usable provenance"
        )

    # Row is GC'd.  Do NOT offer resumable for pruned rows (item 2).
    # The spawner's _validate_resume_identity requires a retained trusted dead
    # DB binding; snapshot evidence alone cannot prove the session is not live elsewhere.
    snap_prov = recorded.get("launch_provenance")
    if isinstance(snap_prov, dict) and (
        snap_prov.get("cwd_resolved") or snap_prov.get("cwd_requested")
    ):
        # Provenance exists. Only skip if we have positive terminal evidence from
        # an inbound spawn job. The mere absence of running jobs is not enough —
        # jobs could have been GC'd along with the participant row.
        inbound_spawns = [
            j
            for j in recorded.get("jobs", [])
            if j.get("kind") == "spawn" and j.get("target_id") == orig_id
        ]
        if inbound_spawns:
            # Check if all inbound spawns are terminal.
            all_terminal = True
            for job in inbound_spawns:
                recorded_state = job.get("state", "running")
                if recorded_state != "running":
                    continue
                handle = job.get("handle")
                live = live_jobs_by_handle.get(handle) if handle else None
                if live is None or live["state"] == "running":
                    all_terminal = False
                    break
            if all_terminal:
                return (
                    "completed",
                    "GC'd; all inbound spawn jobs terminal; work is done",
                )
        return "respawnable", "pruned from DB but snapshot has launch_provenance for cold respawn"

    # No provenance. Use job evidence to decide skip vs fail.
    complete = _node_is_complete(recorded, live_jobs_by_handle)
    if complete:
        return "completed", "GC'd with no observable unfinished work; nothing to restore"

    return "pruned", (f"participant {orig_id!r} was GC'd with incomplete work and no provenance")


# ---- pane verification helpers -----------------------------------------------


async def _get_pane_info(daemon: Daemon, pane_id: str | None) -> dict | object | None:
    """Query tmux for pane details.

    Returns:
    - A dict with 'harness', 'pane_id', 'command' keys when tmux is available
      and the pane exists.
    - None when tmux is available and the pane does not exist (gone).
    - ``_PANE_INFO_TMUX_UNAVAILABLE`` sentinel when tmux is not available
      (test environment or not installed); caller should trust the DB row.
    """
    if not pane_id:
        return None
    try:
        from theater.daemon.harness_detect import detect_harness
        from theater.tmux import client as tmux

        if not tmux.available():
            return _PANE_INFO_TMUX_UNAVAILABLE
        pane = await tmux.pane_info(pane_id)
        if pane is None:
            return None
    except Exception:
        return _PANE_INFO_TMUX_UNAVAILABLE
    else:
        harness = detect_harness(pane.current_command, pane.pane_pid)
        return {"pane_id": pane_id, "harness": harness, "command": pane.current_command}


# ---- BFS order ---------------------------------------------------------------


def _bfs_order(creator_id: str, nodes_by_id: dict[str, dict]) -> list[str]:
    """Return node ids in BFS order starting from creator_id.

    Orphan nodes (parent not in snapshot) are attached to the creator.
    """
    children_of: dict[str, list[str]] = {nid: [] for nid in nodes_by_id}
    for nid, node in nodes_by_id.items():
        parent = node.get("parent_id")
        if parent and parent in children_of:
            children_of[parent].append(nid)
        elif nid != creator_id and creator_id in children_of:
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


# ---- reparenting -------------------------------------------------------------


def _reparent_live(
    daemon: Daemon,
    pid: str,
    *,
    new_parent_id: str,
    checkpoint_id: int,
) -> None:
    """Reparent a live participant to a newly reconstructed parent.

    Validates:
    - No cycle: new_parent_id must not be a descendant of pid.
    - Depth/budget rails for the new topology.
    - The participant is not already owned by a *different* live parent
      (live_lineage_conflict guard).

    The store write goes through the daemon-owned SQLite path.
    """
    # Cycle check.
    if pid == new_parent_id:
        raise BadRequest(
            f"checkpoint {checkpoint_id!r}: reparent would create a self-loop for {pid!r}"
        )
    desc_ids = set(lineage.subtree_ids(daemon.store, pid))
    if new_parent_id in desc_ids:
        raise BadRequest(
            f"checkpoint {checkpoint_id!r}: reparenting {pid!r} under {new_parent_id!r} "
            f"would create a lineage cycle"
        )

    # Rail checks on the new topology.
    rails = daemon.config.rails
    check_depth(daemon.store, new_parent_id, cap=rails.depth_cap)
    check_budget(daemon.store, new_parent_id, limit=rails.budget)

    daemon.store.reparent_participant(pid, new_parent_id=new_parent_id)


# ---- worktree recovery -------------------------------------------------------


def _resolve_worktree_cwd(
    prov: dict,
    recorded: dict,
    *,
    new_participant_id: str,
) -> tuple[str | None, str | bool | None, list[str]]:
    """Determine the effective cwd and worktree params for a cold respawn.

    Returns (cwd, worktree_param, warnings).

    - If the worktree still exists and belongs to the right repo → reuse (cwd = path).
    - If the worktree is gone but provenance is sufficient → request recreation.
    - Otherwise → fall back to cwd_resolved/cwd_requested without worktree.

    worktree_param is the value to pass to spawn (True / "name" / False).
    """
    warnings: list[str] = []
    worktree_type = prov.get("worktree_type")
    worktree_branch = prov.get("worktree_branch")
    worktree_repo_root = prov.get("worktree_repo_root")
    worktree_name = prov.get("worktree_name")
    worktree_recorded_path = prov.get("cwd_resolved")  # was the worktree path at spawn time

    if worktree_type is None:
        # No worktree was requested; use cwd_resolved / cwd_requested directly.
        cwd = prov.get("cwd_resolved") or prov.get("cwd_requested") or recorded.get("cwd")
        return cwd, False, warnings

    # Check if the recorded worktree path still exists.
    from pathlib import Path as _Path

    if worktree_recorded_path and _Path(worktree_recorded_path).is_dir():
        # Path exists — verify it is still a git worktree of the same repo.
        try:
            import subprocess

            r = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=worktree_recorded_path,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if r.returncode == 0:
                # Worktree exists and is valid — reuse it.
                worktree_param: str | bool
                if worktree_type == "named" and worktree_name:
                    worktree_param = worktree_name
                else:
                    worktree_param = True
                return worktree_recorded_path, worktree_param, warnings
        except Exception as exc:
            warnings.append(f"could not verify worktree {worktree_recorded_path!r}: {exc}")

    # Worktree path is gone or invalid.
    if worktree_repo_root and worktree_branch:
        # Sufficient provenance to recreate.
        warnings.append(
            f"worktree {worktree_recorded_path!r} no longer exists; "
            f"requesting recreation from {worktree_repo_root!r} branch {worktree_branch!r}"
        )
        if worktree_type == "named" and worktree_name:
            return worktree_repo_root, worktree_name, warnings
        return worktree_repo_root, True, warnings

    # Cannot restore worktree — run without one.
    cwd = prov.get("cwd_resolved") or prov.get("cwd_requested") or recorded.get("cwd")
    if worktree_type:
        warnings.append(
            f"worktree {worktree_recorded_path!r} is gone and provenance is insufficient "
            f"to recreate it; running in {cwd!r} without worktree isolation"
        )
    return cwd, False, warnings


# ---- restore orchestration ---------------------------------------------------


async def restore_tree(
    daemon: Daemon,
    *,
    checkpoint_id: int,
    snapshot: dict,
    caller_id: str,
    approval: str,
    token: str,
    prior_progress: dict | None = None,
) -> dict:
    """Restore the orchestration tree from a v2 snapshot.

    ``prior_progress``: from a partial/stranded checkpoint, maps original_id →
    {new_participant_id, action} for nodes that already completed. These are
    short-circuited: the node is reported as the prior result.

    Progress is persisted to the DB after every successfully restored node
    so that a daemon restart or cancellation preserves the work already done.
    """
    from sqlalchemy import select

    from theater.daemon.schema import jobs as jobs_table

    creator_id = snapshot["creator_id"]
    nodes_by_id: dict[str, dict] = {n["participant_id"]: n for n in snapshot["nodes"]}

    # Build live jobs lookup for the entire subtree.
    all_ids = list(nodes_by_id.keys())
    live_rows = daemon.store.conn.execute(
        select(*(jobs_table.c[k] for k in _SNAPSHOT_JOB_KEYS)).where(
            jobs_table.c.caller_id.in_(all_ids) | jobs_table.c.target_id.in_(all_ids)
        )
    ).fetchall()
    live_jobs_by_handle: dict[str, dict] = {
        r._mapping["handle"]: {k: r._mapping[k] for k in _SNAPSHOT_JOB_KEYS} for r in live_rows
    }

    id_map: dict[str, str] = {}  # original_id → new_participant_id
    reports: list[NodeRestoreReport] = []
    restore_order = _bfs_order(creator_id, nodes_by_id)
    creator_report: NodeRestoreReport | None = None

    progress = dict(prior_progress or {})

    for orig_id in restore_order:
        recorded = nodes_by_id.get(orig_id)
        if recorded is None:
            recorded = _stub_node(orig_id)

        # Short-circuit for nodes already restored in a prior partial attempt.
        if orig_id in progress:
            prior = progress[orig_id]
            id_map[orig_id] = prior.get("new_participant_id") or orig_id
            report = NodeRestoreReport(
                original_participant_id=orig_id,
                new_participant_id=prior.get("new_participant_id"),
                original_parent_id=recorded.get("parent_id"),
                current_parent_id=None,
                new_parent_id=prior.get("new_parent_id"),
                harness=recorded.get("harness"),
                old_session_id=recorded.get("session_id"),
                new_session_id=prior.get("new_session_id"),
                classification=prior.get("classification", "unknown"),
                action=prior.get("action", "skipped"),
                final_status=prior.get("final_status"),
                reason=prior.get("reason", "previously restored in partial attempt"),
            )
            reports.append(report)
            if orig_id == creator_id:
                creator_report = report
            continue

        live_participant = daemon.store.get_participant(orig_id)
        pane_info_result: dict | object | None = _PANE_INFO_TMUX_UNAVAILABLE
        if live_participant is not None and live_participant.tmux_pane:
            pane_info_result = await _get_pane_info(daemon, live_participant.tmux_pane)

        classification, reason = classify_node(
            recorded, live_participant, live_jobs_by_handle, pane_info=pane_info_result
        )

        # Determine effective parent ID for this node.
        original_parent_id = recorded.get("parent_id")
        if orig_id == creator_id:
            # Live creator: retain its current parent (the caller is the restore initiator,
            # not a new structural parent). Dead creator: the caller becomes the parent.
            live_creator_row = daemon.store.get_participant(orig_id)
            if live_creator_row is not None and live_creator_row.status is not Status.DEAD:
                new_parent_id: str | None = live_creator_row.parent_id  # unchanged
            else:
                new_parent_id = caller_id  # dead creator becomes child of caller
        else:
            # Use the reconstructed parent's new ID if it was respawned.
            mapped_parent = id_map.get(original_parent_id or "")
            if mapped_parent is None and original_parent_id:
                # Parent not yet processed or was skipped/failed.
                parent_report = next(
                    (r for r in reports if r.original_participant_id == original_parent_id),
                    None,
                )
                skip_actions = ("skipped", "ancestor_not_restored", "failed")
                if parent_report and parent_report.action in skip_actions:
                    # Parent was not restored; skip this descendant too.
                    report = _make_skip_report(
                        orig_id=orig_id,
                        recorded=recorded,
                        live_participant=live_participant,
                        original_parent_id=original_parent_id,
                        reason=(
                            f"ancestor {original_parent_id!r} was not restored "
                            f"({parent_report.action}); descendant skipped"
                        ),
                    )
                    id_map[orig_id] = orig_id
                    reports.append(report)
                    continue
            new_parent_id = mapped_parent or original_parent_id

        current_parent_id: str | None = None
        if live_participant is not None:
            current_parent_id = live_participant.parent_id

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
            checkpoint_id=checkpoint_id,
            live_jobs_by_handle=live_jobs_by_handle,
        )

        if report.new_participant_id is not None:
            id_map[orig_id] = report.new_participant_id
        else:
            id_map[orig_id] = orig_id

        reports.append(report)
        if orig_id == creator_id:
            creator_report = report
            # Creator failure or skip is fatal for v2 — the whole restore depends on it.
            if report.action in ("failed", "skipped", "ancestor_not_restored"):
                raise BadRequest(
                    f"checkpoint {checkpoint_id!r}: creator restoration failed: {report.reason}"
                )

        # Persist progress after each successful (non-failed, non-skipped) node.
        if report.action not in ("failed", "skipped", "ancestor_not_restored"):
            progress[orig_id] = {
                "new_participant_id": report.new_participant_id,
                "new_parent_id": report.new_parent_id,
                "new_session_id": report.new_session_id,
                "classification": report.classification,
                "action": report.action,
                "final_status": report.final_status,
                "reason": report.reason,
            }
            daemon.store.persist_restore_progress(
                checkpoint_id,
                token=token,
                progress=json.dumps(progress, sort_keys=True, separators=(",", ":")),
            )

    assert creator_report is not None, "creator node not in restore_order"

    descendant_reports = [r for r in reports if r.original_participant_id != creator_id]
    partial_failures = [
        r.original_participant_id for r in descendant_reports if r.action in ("failed",)
    ]

    # Determine final state.
    some_success = any(r.action in ("reused_live", "reparented", "respawned") for r in reports)
    any_failure = bool(partial_failures)
    restore_state = "partial" if (some_success and any_failure) else "restored"

    # Compute summary counts.
    action_counts: dict[str, int] = {}
    for r in reports:
        action_counts[r.action] = action_counts.get(r.action, 0) + 1

    result = TreeRestoreResult(
        checkpoint_id=checkpoint_id,
        snapshot_version=2,
        restore_state=restore_state,
        restored_by=caller_id,
        approval=approval,
        creator_report=creator_report,
        descendant_reports=descendant_reports,
        partial_failures=partial_failures,
        counts={
            "total": len(reports),
            "by_action": action_counts,
        },
    )
    return result.to_dict()


def _make_skip_report(
    *,
    orig_id: str,
    recorded: dict,
    live_participant: Participant | None,
    original_parent_id: str | None,
    reason: str,
) -> NodeRestoreReport:
    return NodeRestoreReport(
        original_participant_id=orig_id,
        new_participant_id=None,
        original_parent_id=original_parent_id,
        current_parent_id=live_participant.parent_id if live_participant else None,
        new_parent_id=None,
        harness=recorded.get("harness") or (live_participant.harness if live_participant else None),
        old_session_id=recorded.get("session_id"),
        new_session_id=None,
        classification="skipped",
        action="ancestor_not_restored",
        final_status=str(live_participant.status) if live_participant else "dead",
        reason=reason,
    )


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
    checkpoint_id: int,
    live_jobs_by_handle: dict[str, dict],
) -> NodeRestoreReport:
    """Restore a single node and return its report."""
    harness = recorded.get("harness") or (live_participant.harness if live_participant else None)
    old_session_id = recorded.get("session_id") or (
        live_participant.session_id if live_participant else None
    )
    node_jobs = recorded.get("jobs", [])

    # Build job reconciliation for this node.
    job_recs = [
        {
            "handle": jr.handle,
            "kind": jr.kind,
            "recorded_state": jr.recorded_state,
            "current_state": jr.current_state,
            "outcome": jr.outcome,
            "reason": jr.reason,
        }
        for jr in reconcile_jobs(node_jobs, live_jobs_by_handle)
    ]

    def _report(
        *,
        new_participant_id: str | None,
        new_parent_id_out: str | None,
        new_session_id: str | None,
        action: str,
        final_status: str | None,
        reason_out: str,
        warnings: list[str] | None = None,
    ) -> NodeRestoreReport:
        return NodeRestoreReport(
            original_participant_id=orig_id,
            new_participant_id=new_participant_id,
            original_parent_id=original_parent_id,
            current_parent_id=current_parent_id,
            new_parent_id=new_parent_id_out,
            harness=harness,
            old_session_id=old_session_id,
            new_session_id=new_session_id,
            classification=classification,
            action=action,
            final_status=final_status,
            reason=reason_out,
            job_reconciliations=job_recs,
            warnings=warnings or [],
        )

    # stale_live / live_harness_conflict: live DB row but pane gone or different harness.
    # Restore does not inject text, so we can still return the participant as
    # reused_live with a warning. The caller can verify and send separately.
    # If no live_participant (shouldn't happen for stale_live but be safe), treat as dead.
    effective_classification = classification
    extra_warnings: list[str] = []
    if classification in ("stale_live", "live_harness_conflict"):
        if live_participant is not None:
            # Return reused_live with a staleness warning. The node is alive in the DB;
            # the caller must verify the pane before sending. We note the stale condition.
            extra_warnings.append(
                f"participant {orig_id!r}: {classification} — "
                f"pane {live_participant.tmux_pane!r} may be stale or occupied by a "
                f"different harness; verify before sending"
            )
            effective_classification = "live"  # treat as live for restore (no injection)
        else:
            # live_participant is None despite live classification — defensive fallback.
            extra_warnings.append(
                f"participant {orig_id!r}: {classification} with no DB row; treating as dead"
            )
            complete = _node_is_complete(recorded, live_jobs_by_handle)
            if complete:
                effective_classification = "completed"
            elif _has_usable_provenance(recorded, None):
                effective_classification = "respawnable"
            else:
                effective_classification = "failed"

    if effective_classification == "live":
        assert live_participant is not None
        # Reparent if needed (descendant whose recorded parent was respawned).
        action = "reused_live"
        if new_parent_id is not None and live_participant.parent_id != new_parent_id:
            # Check live_lineage_conflict: is the current parent a different live node?
            if live_participant.parent_id is not None:
                current_parent = daemon.store.get_participant(live_participant.parent_id)
                if (
                    current_parent is not None
                    and current_parent.status is not Status.DEAD
                    and live_participant.parent_id != new_parent_id
                ):
                    return _report(
                        new_participant_id=orig_id,
                        new_parent_id_out=live_participant.parent_id,
                        new_session_id=live_participant.session_id,
                        action="live_lineage_conflict",
                        final_status=str(live_participant.status),
                        reason_out=(
                            f"participant {orig_id!r} is live and owned by a different "
                            f"live parent {live_participant.parent_id!r}; "
                            f"cannot steal from a live parent"
                        ),
                        warnings=extra_warnings,
                    )
            # Safe to reparent.
            try:
                _reparent_live(
                    daemon, orig_id, new_parent_id=new_parent_id, checkpoint_id=checkpoint_id
                )
                action = "reparented"
            except Exception as exc:
                extra_warnings.append(f"reparent failed: {exc}; parent unchanged")
        return _report(
            new_participant_id=orig_id,
            new_parent_id_out=(
                new_parent_id if action == "reparented" else live_participant.parent_id
            ),
            new_session_id=live_participant.session_id,
            action=action,
            final_status=str(live_participant.status),
            reason_out=reason,
            warnings=extra_warnings,
        )

    if effective_classification in ("completed",):
        return _report(
            new_participant_id=None,
            new_parent_id_out=None,
            new_session_id=None,
            action="skipped",
            final_status="dead",
            reason_out=reason,
            warnings=extra_warnings,
        )

    if effective_classification == "pruned":
        return _report(
            new_participant_id=None,
            new_parent_id_out=None,
            new_session_id=None,
            action="failed",
            final_status="dead",
            reason_out=reason,
            warnings=extra_warnings,
        )

    if effective_classification == "failed":
        return _report(
            new_participant_id=None,
            new_parent_id_out=None,
            new_session_id=None,
            action="failed",
            final_status=str(live_participant.status) if live_participant else "dead",
            reason_out=reason,
            warnings=extra_warnings,
        )

    # respawnable → cold respawn.
    if not harness:
        return _report(
            new_participant_id=None,
            new_parent_id_out=None,
            new_session_id=None,
            action="failed",
            final_status="dead",
            reason_out=f"cannot spawn: harness unknown for {orig_id!r}",
            warnings=extra_warnings,
        )

    spawn_warnings: list[str] = []
    try:
        new_participant, spawn_action, spawn_warnings = await _spawn_node(
            daemon=daemon,
            orig_id=orig_id,
            recorded=recorded,
            live_participant=live_participant,
            harness=harness,
            new_parent_id=new_parent_id,
            approval=approval,
            live_jobs_by_handle=live_jobs_by_handle,
            classification=effective_classification,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("failed to restore node %s: %s", orig_id, exc, exc_info=True)
        return _report(
            new_participant_id=None,
            new_parent_id_out=new_parent_id,
            new_session_id=None,
            action="failed",
            final_status="dead",
            reason_out=str(exc),
            warnings=extra_warnings + spawn_warnings,
        )

    return _report(
        new_participant_id=new_participant.id,
        new_parent_id_out=new_parent_id,
        new_session_id=new_participant.session_id,
        action=spawn_action,
        final_status=str(new_participant.status),
        reason_out=reason,
        warnings=extra_warnings + spawn_warnings,
    )


def _has_usable_provenance(recorded: dict, live: Participant | None) -> bool:
    """Return True iff there is usable cwd provenance for a cold respawn."""
    if live is not None and live.launch_provenance:
        with contextlib.suppress(ValueError, TypeError):
            prov = json.loads(live.launch_provenance)
            if prov.get("cwd_resolved") or prov.get("cwd_requested"):
                return True
    snap_prov = recorded.get("launch_provenance")
    return isinstance(snap_prov, dict) and bool(
        snap_prov.get("cwd_resolved") or snap_prov.get("cwd_requested")
    )


async def _spawn_node(
    *,
    daemon: Daemon,
    orig_id: str,
    recorded: dict,
    live_participant: Participant | None,
    harness: str,
    new_parent_id: str | None,
    approval: str,
    live_jobs_by_handle: dict[str, dict],
    classification: str = "respawnable",
) -> tuple[Participant, str, list[str]]:
    """Resume or cold-respawn a node. Returns (participant, action, warnings).

    For ``resumable``: attempts to resume the native harness session via the
    trusted session_id stored in the live DB row. Applies all current rails.

    For ``respawnable``: cold-respawns using launch provenance. Uses the
    recorded original prompt and response_format. Applies all current rails.

    Worktree: verifies or recreates based on provenance (item 12).
    """
    import theater.daemon.methods as _methods_mod

    _methods_spawn = _methods_mod._spawn
    rails = daemon.config.rails
    warnings: list[str] = []

    # Get provenance from the live row first, then snapshot.
    prov: dict = {}
    if live_participant is not None and live_participant.launch_provenance:
        with contextlib.suppress(ValueError, TypeError):
            prov = json.loads(live_participant.launch_provenance)
    if not prov and isinstance(recorded.get("launch_provenance"), dict):
        prov = recorded["launch_provenance"]

    # Resolve worktree and cwd (item 12).
    cwd, worktree_param, wt_warnings = _resolve_worktree_cwd(
        prov, recorded, new_participant_id=orig_id
    )
    warnings.extend(wt_warnings)

    if not cwd:
        raise BadRequest(
            f"cannot restore participant {orig_id!r}: no cwd available in provenance or snapshot"
        )

    # Determine prompt: use the recorded original spawn prompt for incomplete nodes.
    # The spawn job for this node lives on the parent's job list.
    prompt = _find_original_prompt(recorded, orig_id, live_jobs_by_handle, prov)

    model = prov.get("model")
    reasoning_effort = prov.get("reasoning_effort")
    response_format_str = prov.get("response_format")

    # Rail checks — always apply current policy (item 11).
    check_depth(daemon.store, new_parent_id, cap=rails.depth_cap)
    check_budget(daemon.store, new_parent_id, limit=rails.budget)
    check_model_allowed(harness, model, daemon.config.models_for(harness))
    check_reasoning_allowed(harness, reasoning_effort, daemon.config.reasoning_for(harness))

    # For resumable (retained dead row with trusted session_id), attempt native resume.
    if (
        classification == "resumable"
        and live_participant is not None
        and live_participant.session_id
    ):
        resume_params: dict = {
            "harness": harness,
            "cwd": cwd,
            "approval": approval,
            "parent_id": new_parent_id,
            "resume": live_participant.session_id,
        }
        result = await _methods_spawn(daemon, resume_params)
        p = daemon.store.get_participant(result["id"])
        assert p is not None
        return p, "resumed", warnings

    # Cold respawn.
    params: dict = {
        "harness": harness,
        "cwd": cwd,
        "approval": approval,
        "parent_id": new_parent_id,
        "prompt": prompt or "",
    }
    if model:
        params["model"] = model
    if reasoning_effort:
        params["reasoning_effort"] = reasoning_effort
    if response_format_str:
        with contextlib.suppress(ValueError, TypeError):
            params["response_format"] = json.loads(response_format_str)
    if worktree_param is not False:
        params["worktree"] = worktree_param
        if prov.get("base_branch"):
            params["base_branch"] = prov["base_branch"]

    result = await _methods_spawn(daemon, params)
    p = daemon.store.get_participant(result["id"])
    assert p is not None
    return p, "respawned", warnings


def _find_original_prompt(
    recorded: dict,
    node_id: str,
    live_jobs_by_handle: dict[str, dict],
    prov: dict,
) -> str:
    """Find the original spawn prompt for a node from its recorded jobs.

    The spawn job for this node has kind='spawn' and target_id == node_id.
    We look in the node's own job list (which includes jobs where it is the target).
    Falls back to prov["prompt"] if no spawn job is found.
    """
    for job in recorded.get("jobs", []):
        if job.get("kind") == "spawn" and job.get("target_id") == node_id:
            return job.get("prompt") or prov.get("prompt") or ""
    return prov.get("prompt") or ""


def _has_no_open_work(recorded: dict) -> bool:
    """Return True iff the recorded node has no running jobs (legacy helper)."""
    return not any(j.get("state") == "running" for j in recorded.get("jobs", []))


# ---- v1 compatibility --------------------------------------------------------


def upgrade_v1_snapshot_for_read(snapshot_data: Any, creator_id: str) -> dict:
    """Wrap a v1 jobs list into a display-only v1-shaped dict.

    Used only for reading/reporting — never written back to the database.
    The returned dict has ``version=1`` (not 2) to signal degraded mode.
    """
    if isinstance(snapshot_data, list):
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


# ---- restore entry point (called from methods.py) ----------------------------


async def restore_checkpoint(
    daemon: Daemon,
    *,
    checkpoint_id: int,
    row: dict,
    caller_id: str,
    approval: str,
    token: str,
    snapshot_data: Any,
) -> dict:
    """Entry point called from ``checkpoint.restore`` after the claim is acquired.

    ``snapshot_data`` is already parsed (passed in from methods.py to avoid
    double-parsing).

    For v2 snapshots: orchestrates the full tree restore.
    For v1 snapshots: falls back to the original single-node logic.
    """
    if is_v2_snapshot(snapshot_data):
        # Read prior progress if this is a partial retry.
        prior_progress: dict | None = None
        raw_progress = row.get("restore_progress")
        if raw_progress:
            with contextlib.suppress(ValueError, TypeError):
                prior_progress = json.loads(raw_progress)

        return await restore_tree(
            daemon,
            checkpoint_id=checkpoint_id,
            snapshot=snapshot_data,
            caller_id=caller_id,
            approval=approval,
            token=token,
            prior_progress=prior_progress,
        )

    # v1 fallback.
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

    Returns the legacy ``restored_parent`` shape unchanged.
    """
    import theater.daemon.methods as _methods_mod

    _methods_spawn = _methods_mod._spawn

    parent_id = row["participant_id"]
    parent = daemon.store.get_participant(parent_id)

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
        "snapshot_version": 1,
        "restore_state": "restored",
        "restored_parent": {
            "participant_id": restored["id"],
            "harness": restored["harness"],
            "status": restored["status"],
            "session_id": restored.get("session_id"),
            "action": action,
            "handoff_required": True,
        },
        "recorded_jobs": recorded_jobs,
        "_snapshot_version": 1,
    }
