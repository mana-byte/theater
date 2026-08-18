"""Orchestration-tree checkpoint recovery.

This module owns the recursive snapshot and multi-node restore logic for
checkpoint v2. Checkpoint v1 (creator-only jobs snapshot) remains readable
and is handled by a compatibility shim that preserves the original behaviour,
which is explicitly documented as degraded (creator only, no descendants).

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
                "jobs": [
                    {
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

Job snapshotting: for each node we capture all jobs where
``caller_id IN subtree OR target_id IN subtree``. The spawn job for a child
lives on both the parent node (caller=parent) and the child node (target=child).
Top-level ``jobs`` in the result deduplicates by handle.

v1 (legacy): flat list of job dicts for the creator only. Degraded mode:
no descendants, no tree restore, no progress. Documented as such.

Action enum (exhaustive)
------------------------
``reused_live``   live DB row, verified pane or tmux unavailable, kept in place
``resumed``       dead retained row, native session resumed
``respawned``     cold-respawned from launch provenance
``skipped``       completed work, or ancestor not restored; nothing to do
``failed``        unrestorable; live_lineage_conflict, reparent failure,
                  stale/mismatch with no safe fallback, EXTERNAL, etc.

Details beyond the action are encoded in ``classification`` and ``reason``.
"partial" is never an action — it is a restore_state on the checkpoint row.

Classification values
---------------------
``live``                pane verified or tmux unavailable; DB says live
``stale_live``          DB says live but tmux confirmed pane gone
``live_harness_conflict`` DB says live but a different harness in pane
``live_reparented``     live node whose parent was updated to mapped parent
``live_lineage_conflict`` live node owned by a different live parent
``resumable``           dead retained row with trusted session_id
``respawnable``         dead or pruned with usable launch_provenance
``completed``           confirmed terminal; nothing to restore
``pruned``              GC'd with incomplete work and no provenance
``failed``              EXTERNAL, no provenance, or otherwise unrestorable

Claim and restore_state semantics
----------------------------------
- ``ready``    → claimable (via claim_checkpoint_restore)
- ``restoring`` → claim held; in-flight
- ``restored`` → terminal; all nodes succeeded
- ``partial``  → terminal; at least one node failed (side effects may exist)
- ``failed``   → terminal; creator itself failed (or stranded without progress)

``partial`` is terminal. It cannot be re-claimed. The progress blob is an
audit record of what was attempted, not a retry mechanism.

Partial-state calculation
--------------------------
Any node whose action is NOT ``reused_live``, ``resumed``, or ``respawned``
is counted as unsuccessful. If any node is unsuccessful (including ``skipped``
due to ancestor failure, ``failed`` for lineage conflicts, etc.) AND at least
one node did succeed, the checkpoint finalises as ``partial``. If no node
succeeded, it finalises as ``failed``.

Progress persistence
--------------------
Progress is persisted to the DB after EVERY node outcome, including failed
and skipped nodes, so cancellation/crash preserves a complete audit record.

Worktree safety
---------------
If a node required a worktree (worktree_type != null in provenance), restore
MUST either verify the existing path OR safely recreate. It must NEVER fall
back to a non-isolated cwd without failing clearly. Verification uses
``worktree.main_repo_root`` (canonical, not linked), expected branch, and
linked-worktree identity. Reuse passes cwd=recorded_path, worktree=False
(no spurious create/join). Recreation uses worktree=True/name plus
base_branch from the immutable worktree_base_commit.

Sends not replayed
------------------
Send jobs appear in the ``job_reconciliations`` list but are never re-issued.
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

# ---- snapshot construction ---------------------------------------------------


def build_tree_snapshot(daemon: Daemon, creator_id: str) -> dict:
    """Build a v2 tree snapshot for *creator_id* and all its descendants."""
    from sqlalchemy import or_, select

    from theater.daemon.schema import jobs as jobs_table

    store = daemon.store
    all_ids = lineage.subtree_ids(store, creator_id)

    nodes: list[dict] = []
    for pid in all_ids:
        p = store.get_participant(pid)
        if p is None:
            nodes.append(_stub_node(pid))
            continue

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
    return isinstance(snapshot_data, dict) and snapshot_data.get("version") == 2


def parse_snapshot(raw: str) -> Any:
    return json.loads(raw or "[]")


def validate_v2_snapshot(snapshot: dict, checkpoint_id: int) -> None:  # noqa: PLR0912
    """Validate snapshot structure. Raises BadRequest on violations.

    All non-creator nodes must have their parent inside the snapshot
    OR have a null/external parent (the creator's original external parent
    may be outside). Dangling orphan links that would cause silent grafting
    onto the creator are rejected.
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

    # Reject dangling parent links on non-creator nodes.
    # Each non-creator node must have parent_id == None, parent_id == creator_id,
    # or parent_id that is itself in the snapshot. Silently grafting orphans onto
    # the creator (old _bfs_order behaviour) is rejected here.
    for n in nodes:
        nid = n["participant_id"]
        if nid == creator_id:
            continue  # creator may have an external parent
        parent = n.get("parent_id")
        if parent is not None and parent not in seen_ids:
            raise BadRequest(
                f"checkpoint {checkpoint_id!r}: node {nid!r} has parent {parent!r} "
                f"which is not in the snapshot; dangling parent links are not allowed"
            )

    # Cycle detection in snapshot parent_id links.
    nodes_by_id = {n["participant_id"]: n for n in nodes}
    for nid in seen_ids:
        visited: set[str] = set()
        cur: str = nid
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
    kind: str
    recorded_state: str
    current_state: str | None
    outcome: str
    reason: str


def reconcile_jobs(
    node_jobs: list[dict],
    live_jobs_by_handle: dict[str, dict],
    *,
    seen_handles: set[str] | None = None,
) -> list[JobReconciliation]:
    """Build a deduplicated job reconciliation list for one node.

    ``seen_handles`` tracks handles already reported by a prior node so
    that spawn jobs appearing on both parent (caller) and child (target)
    are reported only once across the tree. Pass the same set across
    all nodes when building a flat top-level list.
    """
    if seen_handles is None:
        seen_handles = set()
    result: list[JobReconciliation] = []
    for job in node_jobs:
        handle = job.get("handle", "")
        if not handle:
            continue
        kind = job.get("kind", "send")
        recorded_state = job.get("state", "running")
        live = live_jobs_by_handle.get(handle)
        current_state = live["state"] if live else None

        if handle in seen_handles:
            continue  # dedup across parent/child for top-level list
        seen_handles.add(handle)

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
                    reason="spawn job GC'd; target status inferred from participant row",
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
                reason="spawn running at restore time; target node drives recovery",
            )
        )

    return result


# ---- pane verification sentinel ----------------------------------------------

#: Returned by ``_get_pane_info`` when tmux is not available.
#: The classifier should trust the DB row rather than classifying as stale.
_PANE_INFO_TMUX_UNAVAILABLE: object = object()

# ---- node completion check ---------------------------------------------------


def _node_is_complete(recorded: dict, live_jobs_by_handle: dict[str, dict]) -> bool:
    """Return True iff a node has positive evidence of completion.

    Uses inbound spawn job (target = this node) as the primary signal.
    Falls back to checking for any running outbound job if no inbound spawn.
    """
    node_id = recorded["participant_id"]
    jobs = recorded.get("jobs", [])

    inbound_spawns = [j for j in jobs if j.get("kind") == "spawn" and j.get("target_id") == node_id]
    if inbound_spawns:
        for job in inbound_spawns:
            if job.get("state", "running") != "running":
                continue
            handle = job.get("handle")
            live = live_jobs_by_handle.get(handle) if handle else None
            if live is None or live["state"] == "running":
                return False
        return True

    for job in jobs:
        if job.get("state", "running") != "running":
            continue
        handle = job.get("handle")
        if handle is None:
            return False
        live = live_jobs_by_handle.get(handle)
        if live is None or live["state"] == "running":
            return False

    return True


# ---- restore report ----------------------------------------------------------


@dataclass
class NodeRestoreReport:
    """Per-participant restore outcome.

    ``action`` is one of the five public values:
        reused_live | resumed | respawned | skipped | failed

    ``classification`` carries the finer reason:
        live | live_reparented | live_lineage_conflict | stale_live |
        live_harness_conflict | resumable | respawnable | completed |
        pruned | failed | ancestor_skipped
    """

    original_participant_id: str
    new_participant_id: str | None  # None for skipped/failed
    original_parent_id: str | None
    current_parent_id: str | None  # current DB parent at restore time
    new_parent_id: str | None  # final parent in restored tree
    harness: str | None
    original_session_id: str | None  # session_id at checkpoint time
    current_session_id: str | None  # session_id of new/reused participant
    classification: str
    action: str
    final_status: str | None
    reason: str
    job_reconciliations: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---- actions -----------------------------------------------------------------

#: The five public action codes for v2 restore.
_SUCCESS_ACTIONS = frozenset({"reused_live", "resumed", "respawned"})
_TERMINAL_ACTIONS = frozenset({"reused_live", "resumed", "respawned", "skipped", "failed"})


def _action_is_success(action: str) -> bool:
    return action in _SUCCESS_ACTIONS


@dataclass
class TreeRestoreResult:
    checkpoint_id: int
    snapshot_version: int
    restore_state: str  # restored | partial | failed
    restored_by: str
    approval: str
    creator_report: NodeRestoreReport
    descendant_reports: list[NodeRestoreReport]
    counts: dict = field(default_factory=dict)

    @property
    def all_reports(self) -> list[NodeRestoreReport]:
        return [self.creator_report, *self.descendant_reports]

    @property
    def partial_failures(self) -> list[str]:
        """Participant IDs with failed or ancestor-skipped outcomes."""
        return [
            r.original_participant_id
            for r in self.all_reports
            if r.action == "failed"
            or (r.action == "skipped" and r.classification == "ancestor_skipped")
        ]

    def to_dict(self, *, all_jobs: list[dict] | None = None) -> dict:
        reports = self.all_reports
        return {
            "checkpoint_id": self.checkpoint_id,
            "snapshot_version": self.snapshot_version,
            "restore_state": self.restore_state,
            "restored_by": self.restored_by,
            "approval": self.approval,
            "counts": self.counts,
            # Top-level jobs list (deduplicated by handle across all nodes).
            "jobs": all_jobs or [],
            # Flat participants list (canonical).
            "participants": [_node_report_to_dict(r) for r in reports],
            # Keep convenience aliases for existing callers.
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
        "original_session_id": r.original_session_id,
        "current_session_id": r.current_session_id,
        "classification": r.classification,
        "action": r.action,
        "final_status": r.final_status,
        "reason": r.reason,
        "job_reconciliations": r.job_reconciliations,
        "warnings": r.warnings,
    }


# ---- classification ----------------------------------------------------------


def classify_node(  # noqa: PLR0912
    recorded: dict,
    live_participant: Participant | None,
    live_jobs_by_handle: dict[str, dict],
    *,
    pane_info: dict | object | None = _PANE_INFO_TMUX_UNAVAILABLE,
) -> tuple[str, str]:
    """Return (classification, reason) for a recorded snapshot node.

    ``pane_info``:
      - dict with 'harness' key: tmux confirmed pane exists.
      - None: tmux confirmed pane is gone → ``stale_live``.
      - ``_PANE_INFO_TMUX_UNAVAILABLE``: tmux not queryable; trust DB row.

    stale_live and live_harness_conflict are never reused as live; they are
    treated as dead and classified further using provenance/session rules.
    """
    orig_id = recorded["participant_id"]

    if live_participant is not None:
        if live_participant.status is not Status.DEAD:
            tier = live_participant.tier
            if tier is Tier.EXTERNAL:
                return "failed", f"participant {orig_id!r} is live but EXTERNAL (no pane)"

            if not live_participant.tmux_pane:
                # No pane recorded on a live spawned node.
                return "stale_live", (f"participant {orig_id!r} is live but has no pane recorded")

            if pane_info is _PANE_INFO_TMUX_UNAVAILABLE:
                return "live", "participant is live (tmux not queryable; trusting DB)"
            if pane_info is None:
                # tmux confirmed pane is gone — treat as dead.
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
        # stale_live/live_harness_conflict states are not reachable here
        # (those only apply to live-status rows above).
        node_jobs = recorded.get("jobs", [])
        has_inbound_spawn = any(
            j.get("kind") == "spawn" and j.get("target_id") == orig_id for j in node_jobs
        )
        if has_inbound_spawn and _node_is_complete(recorded, live_jobs_by_handle):
            return "completed", "dead with all inbound spawn jobs terminal; work is done"

        # Check for native resume (retained trusted session).
        if live_participant.session_id and is_trusted_provenance(
            live_participant.session_correlation
        ):
            return "resumable", (f"dead with trusted session_id {live_participant.session_id!r}")

        # Cold respawn via launch provenance.
        if live_participant.launch_provenance:
            with contextlib.suppress(ValueError, TypeError):
                prov = json.loads(live_participant.launch_provenance)
                if prov.get("cwd_resolved") or prov.get("cwd_requested"):
                    return "respawnable", "dead with launch_provenance; cold respawn available"

        snap_prov = recorded.get("launch_provenance")
        if isinstance(snap_prov, dict) and (
            snap_prov.get("cwd_resolved") or snap_prov.get("cwd_requested")
        ):
            return "respawnable", "dead; using snapshot launch_provenance for cold respawn"

        no_running = not any(j.get("state") == "running" for j in node_jobs)
        if no_running:
            return "completed", "dead with no running jobs and no provenance; nothing to restore"

        return "failed", (
            f"participant {orig_id!r} is dead with incomplete work but no usable provenance"
        )

    # Row is GC'd. Never resumable from snapshot alone.
    snap_prov = recorded.get("launch_provenance")
    if isinstance(snap_prov, dict) and (
        snap_prov.get("cwd_resolved") or snap_prov.get("cwd_requested")
    ):
        inbound_spawns = [
            j
            for j in recorded.get("jobs", [])
            if j.get("kind") == "spawn" and j.get("target_id") == orig_id
        ]
        if inbound_spawns:
            all_terminal = True
            for job in inbound_spawns:
                if job.get("state", "running") != "running":
                    continue
                handle = job.get("handle")
                live = live_jobs_by_handle.get(handle) if handle else None
                if live is None or live["state"] == "running":
                    all_terminal = False
                    break
            if all_terminal:
                return "completed", "GC'd; all inbound spawn jobs terminal; work is done"
        return "respawnable", "pruned from DB but snapshot has launch_provenance"

    complete = _node_is_complete(recorded, live_jobs_by_handle)
    if complete:
        return "completed", "GC'd with no observable unfinished work; nothing to restore"

    return "pruned", f"participant {orig_id!r} was GC'd with incomplete work and no provenance"


# ---- dead-node classification for stale/mismatch ----------------------------


def _classify_as_dead(
    recorded: dict,
    live_participant: Participant | None,
    live_jobs_by_handle: dict[str, dict],
) -> tuple[str, str]:
    """Classify a stale_live or live_harness_conflict node using dead-node rules."""
    orig_id = recorded["participant_id"]
    node_jobs = recorded.get("jobs", [])

    has_inbound_spawn = any(
        j.get("kind") == "spawn" and j.get("target_id") == orig_id for j in node_jobs
    )
    if has_inbound_spawn and _node_is_complete(recorded, live_jobs_by_handle):
        return "completed", "stale pane; all inbound spawn jobs terminal; work is done"

    # Try session resume from the live row.
    if (
        live_participant is not None
        and live_participant.session_id
        and is_trusted_provenance(live_participant.session_correlation)
    ):
        return "resumable", (
            f"stale pane but trusted session_id {live_participant.session_id!r} available"
        )

    # Try cold respawn from live row provenance.
    if live_participant is not None and live_participant.launch_provenance:
        with contextlib.suppress(ValueError, TypeError):
            prov = json.loads(live_participant.launch_provenance)
            if prov.get("cwd_resolved") or prov.get("cwd_requested"):
                return "respawnable", "stale pane; using retained launch_provenance"

    # Snapshot provenance.
    snap_prov = recorded.get("launch_provenance")
    if isinstance(snap_prov, dict) and (
        snap_prov.get("cwd_resolved") or snap_prov.get("cwd_requested")
    ):
        return "respawnable", "stale pane; using snapshot launch_provenance"

    no_running = not any(j.get("state") == "running" for j in node_jobs)
    if no_running:
        return "completed", "stale pane; no running jobs and no provenance; nothing to restore"

    return "failed", (f"participant {orig_id!r}: stale pane, incomplete work, no usable provenance")


# ---- pane verification helpers -----------------------------------------------


async def _get_pane_info(daemon: Daemon, pane_id: str | None) -> dict | object | None:
    """Query tmux for pane details.

    Returns:
    - dict with 'harness' key: pane exists and harness detected.
    - None: tmux available but pane not found (gone).
    - _PANE_INFO_TMUX_UNAVAILABLE: tmux not queryable; caller trusts DB row.
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

    Only follows parent_id links that point to nodes inside the snapshot.
    Nodes with no parent in the snapshot (other than creator) are placed
    at depth-1 under the creator.
    """
    children_of: dict[str, list[str]] = {nid: [] for nid in nodes_by_id}
    for nid, node in nodes_by_id.items():
        parent = node.get("parent_id")
        if parent and parent in children_of:
            children_of[parent].append(nid)
        elif nid != creator_id and creator_id in children_of:
            # validated: only the creator is allowed to have an external parent
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
    """Reparent a live participant. Raises BadRequest on any violation."""
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
    rails = daemon.config.rails
    check_depth(daemon.store, new_parent_id, cap=rails.depth_cap)
    check_budget(daemon.store, new_parent_id, limit=rails.budget)
    daemon.store.reparent_participant(pid, new_parent_id=new_parent_id)


# ---- worktree recovery -------------------------------------------------------


def _resolve_worktree_cwd(  # noqa: PLR0912
    prov: dict,
    recorded: dict,
    *,
    new_participant_id: str,
) -> tuple[str | None, str | bool, list[str]]:
    """Determine the effective cwd and worktree params for a cold respawn.

    Returns (cwd, worktree_param, warnings).

    Safety contract:
    - If the node required a worktree (worktree_type != null), we MUST either
      verify the existing path or safely recreate. Silent fallback to a plain
      cwd is not allowed — the function returns (None, False, warnings) with
      an error warning to signal failure.
    - Path verification uses main_repo_root (canonical) + expected branch.
    - Reuse passes (cwd=recorded_path, worktree_param=False) — the path already
      IS the worktree; passing worktree=True would make _spawn try to create
      another one from inside it.
    - Recreation passes (cwd=repo_root, worktree_param=True/name) so _spawn
      creates the worktree correctly.
    """
    warnings: list[str] = []
    worktree_type = prov.get("worktree_type")
    worktree_branch = prov.get("worktree_branch")
    worktree_repo_root = prov.get("worktree_repo_root")
    worktree_name = prov.get("worktree_name")
    worktree_base_commit = prov.get("worktree_base_commit")
    worktree_recorded_path = prov.get("cwd_resolved")

    if worktree_type is None:
        cwd = prov.get("cwd_resolved") or prov.get("cwd_requested") or recorded.get("cwd")
        return cwd, False, warnings

    # Node required a worktree. Try to verify or recreate it.
    import subprocess
    from pathlib import Path as _Path

    if worktree_recorded_path and _Path(worktree_recorded_path).is_dir():
        # Verify using main_repo_root (canonical), not show-toplevel (linked).
        try:
            from theater.daemon import worktree as worktree_mod

            canonical_root = worktree_mod.main_repo_root(worktree_recorded_path)
            if canonical_root and worktree_repo_root and canonical_root != worktree_repo_root:
                warnings.append(
                    f"worktree {worktree_recorded_path!r}: canonical root "
                    f"{canonical_root!r} differs from recorded {worktree_repo_root!r}; "
                    f"path may be a different repo"
                )
                # Don't trust this path.
            elif canonical_root or not worktree_repo_root:
                # Verify expected branch is checked out.
                branch_ok = True
                if worktree_branch:
                    r = subprocess.run(
                        ["git", "symbolic-ref", "--short", "HEAD"],
                        cwd=worktree_recorded_path,
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    if r.returncode == 0:
                        current_branch = r.stdout.strip()
                        if current_branch != worktree_branch:
                            warnings.append(
                                f"worktree {worktree_recorded_path!r}: expected branch "
                                f"{worktree_branch!r} but found {current_branch!r}"
                            )
                            branch_ok = False
                    else:
                        warnings.append(
                            f"worktree {worktree_recorded_path!r}: could not read current branch"
                        )
                        branch_ok = False

                if branch_ok:
                    # Pass cwd=path, worktree_param=False — don't re-create.
                    return worktree_recorded_path, False, warnings
        except Exception as exc:
            warnings.append(f"could not verify worktree {worktree_recorded_path!r}: {exc}")

    # Path is gone or verification failed. Try recreation.
    if worktree_repo_root and worktree_branch:
        if worktree_base_commit:
            warnings.append(
                f"worktree {worktree_recorded_path!r} gone; recreating from "
                f"{worktree_repo_root!r} at commit {worktree_base_commit!r}"
            )
        else:
            warnings.append(
                f"worktree {worktree_recorded_path!r} gone; recreating from "
                f"{worktree_repo_root!r} (no base_commit; using HEAD)"
            )
        if worktree_type == "named" and worktree_name:
            return worktree_repo_root, worktree_name, warnings
        # For unique worktrees: pass base_branch=base_commit for reproducible recreation.
        return worktree_repo_root, True, warnings

    # Cannot restore worktree — fail clearly rather than silently dropping isolation.
    msg = (
        f"node required worktree (type={worktree_type!r}) but path "
        f"{worktree_recorded_path!r} is missing and provenance is insufficient to recreate; "
        f"need worktree_repo_root and worktree_branch"
    )
    warnings.append(msg)
    return None, False, warnings  # None cwd signals failure to caller


# ---- topology preflight ------------------------------------------------------


def preflight_topology(
    daemon: Daemon,
    *,
    checkpoint_id: int,
    snapshot: dict,
    caller_id: str,
) -> None:
    """Simulate depth/budget/cycle constraints for the full reconstructed tree.

    Called before claiming. Does not mutate state. Raises BadRequest if any
    predictable rail violation would prevent a successful restore.
    """
    creator_id = snapshot["creator_id"]
    nodes_by_id = {n["participant_id"]: n for n in snapshot["nodes"]}
    rails = daemon.config.rails

    # Determine creator's effective parent for depth calculation.
    live_creator = daemon.store.get_participant(creator_id)
    if live_creator is not None and live_creator.status is not Status.DEAD:
        creator_parent = live_creator.parent_id
    else:
        creator_parent = caller_id

    # BFS to check depth/budget for each node that would be (re)spawned.
    # For live nodes we don't spawn, so no rail increase.
    order = _bfs_order(creator_id, nodes_by_id)
    for orig_id in order:
        recorded = nodes_by_id.get(orig_id, _stub_node(orig_id))
        live_p = daemon.store.get_participant(orig_id)
        if live_p is not None and live_p.status is not Status.DEAD:
            continue  # live: no spawn needed, no rail check

        # Would spawn: check parent's depth.
        if orig_id == creator_id:
            effective_parent = creator_parent
        else:
            parent_id = recorded.get("parent_id")
            effective_parent = parent_id  # approximate; id_map not available yet
        try:
            check_depth(daemon.store, effective_parent, cap=rails.depth_cap)
            check_budget(daemon.store, effective_parent, limit=rails.budget)
        except BadRequest as exc:
            raise BadRequest(
                f"checkpoint {checkpoint_id!r}: preflight rail check failed "
                f"for node {orig_id!r}: {exc}"
            ) from exc


# ---- restore orchestration ---------------------------------------------------


async def restore_tree(  # noqa: PLR0912, PLR0915
    daemon: Daemon,
    *,
    checkpoint_id: int,
    snapshot: dict,
    caller_id: str,
    approval: str,
    token: str,
) -> dict:
    """Restore the orchestration tree from a v2 snapshot.

    Progress is persisted after EVERY node outcome (success, failure, skip)
    so that cancellation/crash preserves a complete audit record.

    ``partial`` is a terminal state — this function never reads prior_progress
    to retry. The progress blob is audit-only.
    """
    from sqlalchemy import select

    from theater.daemon.schema import jobs as jobs_table

    creator_id = snapshot["creator_id"]
    nodes_by_id: dict[str, dict] = {n["participant_id"]: n for n in snapshot["nodes"]}

    all_ids = list(nodes_by_id.keys())
    live_rows = daemon.store.conn.execute(
        select(*(jobs_table.c[k] for k in _SNAPSHOT_JOB_KEYS)).where(
            jobs_table.c.caller_id.in_(all_ids) | jobs_table.c.target_id.in_(all_ids)
        )
    ).fetchall()
    live_jobs_by_handle: dict[str, dict] = {
        r._mapping["handle"]: {k: r._mapping[k] for k in _SNAPSHOT_JOB_KEYS} for r in live_rows
    }

    id_map: dict[str, str] = {}
    reports: list[NodeRestoreReport] = []
    restore_order = _bfs_order(creator_id, nodes_by_id)
    creator_report: NodeRestoreReport | None = None
    progress: dict[str, dict] = {}

    def _persist_progress() -> None:
        daemon.store.persist_restore_progress(
            checkpoint_id,
            token=token,
            progress=json.dumps(progress, sort_keys=True, separators=(",", ":")),
        )

    for orig_id in restore_order:
        recorded = nodes_by_id.get(orig_id) or _stub_node(orig_id)

        live_participant = daemon.store.get_participant(orig_id)
        pane_info_result: dict | object | None = _PANE_INFO_TMUX_UNAVAILABLE
        if live_participant is not None and live_participant.tmux_pane:
            pane_info_result = await _get_pane_info(daemon, live_participant.tmux_pane)

        classification, reason = classify_node(
            recorded, live_participant, live_jobs_by_handle, pane_info=pane_info_result
        )

        # For stale_live and live_harness_conflict: reclassify using dead-node rules.
        if classification in ("stale_live", "live_harness_conflict"):
            classification, reason = _classify_as_dead(
                recorded, live_participant, live_jobs_by_handle
            )

        # Determine effective parent for this node.
        original_parent_id = recorded.get("parent_id")
        if orig_id == creator_id:
            live_cr = daemon.store.get_participant(orig_id)
            if live_cr is not None and live_cr.status is not Status.DEAD:
                new_parent_id: str | None = live_cr.parent_id  # live creator keeps its parent
            else:
                new_parent_id = caller_id
        else:
            mapped_parent = id_map.get(original_parent_id or "")
            if mapped_parent is None and original_parent_id:
                parent_report = next(
                    (r for r in reports if r.original_participant_id == original_parent_id),
                    None,
                )
                if parent_report and not _action_is_success(parent_report.action):
                    # Ancestor not successfully restored → skip descendant.
                    report = _make_skip_report(
                        orig_id=orig_id,
                        recorded=recorded,
                        live_participant=live_participant,
                        original_parent_id=original_parent_id,
                        reason=(
                            f"ancestor {original_parent_id!r} was not restored "
                            f"(action={parent_report.action!r}); descendant skipped"
                        ),
                    )
                    id_map[orig_id] = orig_id
                    reports.append(report)
                    if orig_id == creator_id:
                        creator_report = report
                    # Persist every outcome including skip.
                    progress[orig_id] = _report_to_progress(report)
                    _persist_progress()
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

        # Persist after EVERY node outcome (audit record, not retry state).
        progress[orig_id] = _report_to_progress(report)
        _persist_progress()

        # Creator failure is fatal — persist then raise.
        if orig_id == creator_id and not _action_is_success(report.action):
            raise BadRequest(
                f"checkpoint {checkpoint_id!r}: creator restoration failed: {report.reason}"
            )

    assert creator_report is not None, "creator node not in restore_order"

    descendant_reports = [r for r in reports if r.original_participant_id != creator_id]

    # Restore-state calculation.
    # Success: reused_live | resumed | respawned
    # Benign skip: skipped with classification=completed (work was already done)
    # Problem: skipped with classification=ancestor_skipped, failed, or live_lineage_conflict
    def _is_problem(r: NodeRestoreReport) -> bool:
        return r.action == "failed" or (
            r.action == "skipped" and r.classification == "ancestor_skipped"
        )

    successful = [r for r in reports if _action_is_success(r.action)]
    problems = [r for r in reports if _is_problem(r)]
    if problems and successful:
        restore_state = "partial"
    elif problems and not successful:
        restore_state = "failed"
    else:
        restore_state = "restored"

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
        counts={
            "total": len(reports),
            "successful": len(successful),
            "problems": len(problems),
            "by_action": action_counts,
        },
    )

    # Build deduplicated top-level jobs list.
    seen_handles: set[str] = set()
    all_jobs_flat: list[dict] = []
    for r_dict in result.all_reports:
        orig = next(
            (n for n in snapshot["nodes"] if n["participant_id"] == r_dict.original_participant_id),
            None,
        )
        if orig:
            for jr in reconcile_jobs(
                orig.get("jobs", []), live_jobs_by_handle, seen_handles=seen_handles
            ):
                all_jobs_flat.append(
                    {
                        "handle": jr.handle,
                        "kind": jr.kind,
                        "recorded_state": jr.recorded_state,
                        "current_state": jr.current_state,
                        "outcome": jr.outcome,
                        "reason": jr.reason,
                    }
                )

    return result.to_dict(all_jobs=all_jobs_flat)


def _report_to_progress(report: NodeRestoreReport) -> dict:
    return {
        "new_participant_id": report.new_participant_id,
        "new_parent_id": report.new_parent_id,
        "current_session_id": report.current_session_id,
        "classification": report.classification,
        "action": report.action,
        "final_status": report.final_status,
        "reason": report.reason,
    }


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
        original_session_id=recorded.get("session_id"),
        current_session_id=None,
        classification="ancestor_skipped",
        action="skipped",
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
    original_session_id = recorded.get("session_id") or (
        live_participant.session_id if live_participant else None
    )
    node_jobs = recorded.get("jobs", [])

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
        current_session_id: str | None,
        action: str,
        classification_out: str,
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
            original_session_id=original_session_id,
            current_session_id=current_session_id,
            classification=classification_out,
            action=action,
            final_status=final_status,
            reason=reason_out,
            job_reconciliations=job_recs,
            warnings=warnings or [],
        )

    if classification == "live":
        assert live_participant is not None
        # Attempt reparenting if needed.
        action = "reused_live"
        final_classification = "live"
        reparent_warnings: list[str] = []

        if new_parent_id is not None and new_parent_id not in (live_participant.parent_id, orig_id):
            current_p = daemon.store.get_participant(live_participant.parent_id or "")
            if (
                current_p is not None
                and current_p.status is not Status.DEAD
                and live_participant.parent_id != new_parent_id
            ):
                # Different live parent — live_lineage_conflict.
                return _report(
                    new_participant_id=orig_id,
                    new_parent_id_out=live_participant.parent_id,
                    current_session_id=live_participant.session_id,
                    action="failed",
                    classification_out="live_lineage_conflict",
                    final_status=str(live_participant.status),
                    reason_out=(
                        f"participant {orig_id!r} is live and owned by a different "
                        f"live parent {live_participant.parent_id!r}; cannot steal"
                    ),
                )
            # Attempt reparent.
            try:
                # Recheck state immediately before mutation (close races).
                current_live = daemon.store.get_participant(orig_id)
                if current_live is None or current_live.status is Status.DEAD:
                    return _report(
                        new_participant_id=None,
                        new_parent_id_out=None,
                        current_session_id=None,
                        action="failed",
                        classification_out="failed",
                        final_status="dead",
                        reason_out=f"participant {orig_id!r} died between classify and reparent",
                    )
                _reparent_live(
                    daemon, orig_id, new_parent_id=new_parent_id, checkpoint_id=checkpoint_id
                )
                final_classification = "live_reparented"
                action = "reused_live"
            except Exception as exc:
                # Reparent failure → action=failed; descendant can't attach here.
                return _report(
                    new_participant_id=None,
                    new_parent_id_out=None,
                    current_session_id=live_participant.session_id,
                    action="failed",
                    classification_out="failed",
                    final_status=str(live_participant.status),
                    reason_out=f"reparent of {orig_id!r} failed: {exc}",
                    warnings=reparent_warnings,
                )

        return _report(
            new_participant_id=orig_id,
            new_parent_id_out=(
                new_parent_id
                if final_classification == "live_reparented"
                else live_participant.parent_id
            ),
            current_session_id=live_participant.session_id,
            action=action,
            classification_out=final_classification,
            final_status=str(live_participant.status),
            reason_out=reason,
            warnings=reparent_warnings,
        )

    if classification in ("completed",):
        return _report(
            new_participant_id=None,
            new_parent_id_out=None,
            current_session_id=None,
            action="skipped",
            classification_out=classification,
            final_status="dead",
            reason_out=reason,
        )

    if classification in ("pruned", "failed"):
        return _report(
            new_participant_id=None,
            new_parent_id_out=None,
            current_session_id=None,
            action="failed",
            classification_out=classification,
            final_status=str(live_participant.status) if live_participant else "dead",
            reason_out=reason,
        )

    # resumable or respawnable → need to spawn.
    if not harness:
        return _report(
            new_participant_id=None,
            new_parent_id_out=None,
            current_session_id=None,
            action="failed",
            classification_out="failed",
            final_status="dead",
            reason_out=f"cannot spawn: harness unknown for {orig_id!r}",
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
            classification=classification,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("failed to restore node %s: %s", orig_id, exc, exc_info=True)
        return _report(
            new_participant_id=None,
            new_parent_id_out=new_parent_id,
            current_session_id=None,
            action="failed",
            classification_out=classification,
            final_status="dead",
            reason_out=str(exc),
            warnings=spawn_warnings,
        )

    return _report(
        new_participant_id=new_participant.id,
        new_parent_id_out=new_parent_id,
        current_session_id=new_participant.session_id,
        action=spawn_action,
        classification_out=classification,
        final_status=str(new_participant.status),
        reason_out=reason,
        warnings=spawn_warnings,
    )


def _has_usable_provenance(recorded: dict, live: Participant | None) -> bool:
    if live is not None and live.launch_provenance:
        with contextlib.suppress(ValueError, TypeError):
            prov = json.loads(live.launch_provenance)
            if prov.get("cwd_resolved") or prov.get("cwd_requested"):
                return True
    snap_prov = recorded.get("launch_provenance")
    return isinstance(snap_prov, dict) and bool(
        snap_prov.get("cwd_resolved") or snap_prov.get("cwd_requested")
    )


async def _spawn_node(  # noqa: PLR0915
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
    """Resume or cold-respawn a node. Returns (participant, action, warnings)."""
    import theater.daemon.methods as _methods_mod

    _methods_spawn = _methods_mod._spawn
    rails = daemon.config.rails
    warnings: list[str] = []

    prov: dict = {}
    if live_participant is not None and live_participant.launch_provenance:
        with contextlib.suppress(ValueError, TypeError):
            prov = json.loads(live_participant.launch_provenance)
    if not prov and isinstance(recorded.get("launch_provenance"), dict):
        prov = recorded["launch_provenance"]

    # Rail checks (always current policy).
    check_depth(daemon.store, new_parent_id, cap=rails.depth_cap)
    check_budget(daemon.store, new_parent_id, limit=rails.budget)

    # Native resume (retained dead row with trusted session).
    live_session = live_participant.session_id if live_participant else None
    if classification == "resumable" and live_session:
        resume_cwd = (
            prov.get("cwd_resolved") or prov.get("cwd_requested") or recorded.get("cwd") or ""
        )
        model = prov.get("model")
        reasoning_effort = prov.get("reasoning_effort")
        check_model_allowed(harness, model, daemon.config.models_for(harness))
        check_reasoning_allowed(harness, reasoning_effort, daemon.config.reasoning_for(harness))
        result = await _methods_spawn(
            daemon,
            {
                "harness": harness,
                "cwd": resume_cwd,
                "approval": approval,
                "parent_id": new_parent_id,
                "resume": live_session,
            },
        )
        p = daemon.store.get_participant(result["id"])
        assert p is not None
        return p, "resumed", warnings

    # Cold respawn.
    cwd, worktree_param, wt_warnings = _resolve_worktree_cwd(
        prov, recorded, new_participant_id=orig_id
    )
    warnings.extend(wt_warnings)

    worktree_type = prov.get("worktree_type")
    if worktree_type is not None and cwd is None:
        # Worktree required but could not be verified/recreated.
        raise BadRequest(
            f"node {orig_id!r} required worktree isolation but the recorded worktree "
            f"cannot be verified or recreated; " + (wt_warnings[-1] if wt_warnings else "")
        )

    if not cwd:
        cwd = prov.get("cwd_resolved") or prov.get("cwd_requested") or recorded.get("cwd")

    if not cwd:
        raise BadRequest(
            f"cannot restore participant {orig_id!r}: no cwd available in provenance or snapshot"
        )

    model = prov.get("model")
    reasoning_effort = prov.get("reasoning_effort")
    response_format_str = prov.get("response_format")

    check_model_allowed(harness, model, daemon.config.models_for(harness))
    check_reasoning_allowed(harness, reasoning_effort, daemon.config.reasoning_for(harness))

    prompt = _find_original_prompt(recorded, orig_id, live_jobs_by_handle, prov)

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
        # For unique worktree recreation, use immutable base_commit as base_branch
        # so the recreated branch starts at the same commit.
        base_commit = prov.get("worktree_base_commit")
        base_branch = base_commit or prov.get("base_branch")
        if base_branch:
            params["base_branch"] = base_branch

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
    for job in recorded.get("jobs", []):
        if job.get("kind") == "spawn" and job.get("target_id") == node_id:
            return job.get("prompt") or prov.get("prompt") or ""
    return prov.get("prompt") or ""


# ---- v1 compatibility --------------------------------------------------------


def upgrade_v1_snapshot_for_read(snapshot_data: Any, creator_id: str) -> dict:
    """Wrap a v1 jobs list into a display-only v1-shaped dict (degraded mode)."""
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
    """Dispatch to v2 or v1 restore. Called after the claim is acquired."""
    if is_v2_snapshot(snapshot_data):
        return await restore_tree(
            daemon,
            checkpoint_id=checkpoint_id,
            snapshot=snapshot_data,
            caller_id=caller_id,
            approval=approval,
            token=token,
        )

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
    """V1 (creator-only) restore — degraded mode.

    Explicitly documented as degraded: restores creator only, no descendants,
    no tree structure. Returns the legacy ``restored_parent`` shape.
    """
    import theater.daemon.methods as _methods_mod

    _methods_spawn = _methods_mod._spawn

    parent_id = row["participant_id"]
    parent = daemon.store.get_participant(parent_id)

    if parent is None:
        raise BadRequest(
            f"checkpoint {checkpoint_id!r}: creator {parent_id!r} has been pruned "
            f"(v1 degraded mode: cannot restore from snapshot alone)"
        )
    if str(parent.tier) == str(Tier.EXTERNAL):
        raise BadRequest(
            f"checkpoint {checkpoint_id!r}: creator {parent_id!r} is EXTERNAL (v1 degraded mode)"
        )

    parent_is_live = parent.status is not Status.DEAD

    if parent_is_live:
        action = "reused_live"
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
        "_degraded": True,
        "_degraded_reason": (
            "v1 checkpoint: creator-only restore; no descendants recorded or recovered"
        ),
        "restored_parent": {
            "participant_id": restored["id"],
            "harness": restored["harness"],
            "status": restored["status"],
            "original_session_id": parent.session_id,
            "current_session_id": restored.get("session_id"),
            "action": action,
            "handoff_required": True,
        },
        "recorded_jobs": recorded_jobs,
        "_snapshot_version": 1,
    }
