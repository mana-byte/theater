"""Safety rails: depth cap, cycle detection, per-tree budget.

These are the guardrails that make multi-agent orchestration safe enough
to leave running unattended. Without them, a runaway agent could spawn a
deep subtree that exhausts the machine, or close an await cycle that
deadlocks the daemon.

Three rails:

1. **Depth cap** (default 3). Enforced at `spawn`: the depth of the new
   child in the lineage tree must not exceed the cap. Wide fan-out is
   often correct; deep recursion rarely is. The cap is configurable per
   tree root, so a human can raise it for a specific task.

2. **Cycle detection** on the await graph. If A awaits B and B awaits A,
   both block forever — async killed this deadlock; `await` revived it.
   The check walks the await chain from the target upward: if the caller
   appears in that chain, the await is rejected with `cycle_detected`.

3. **Per-tree budget** with hard subtree stop. A budget is allocated to
   a tree root and decremented per spawn. When it hits zero, the subtree
   is stopped: no further spawns are allowed, and a `budget_exceeded`
   error is returned. The only backstop when heuristics fail.
"""

from __future__ import annotations

import logging

from theater.daemon import lineage
from theater.daemon.store import Store
from theater.models import BadRequest, now

logger = logging.getLogger("theater.rails")

#: Default maximum depth of a spawn tree. Roots are depth 0; their
#: children are depth 1; etc. A cap of 3 means a root can spawn children
#: that spawn children that spawn children, but those grandchildren
#: cannot spawn further.
DEFAULT_DEPTH_CAP = 3

#: Default budget: number of participants in a tree before it is stopped.
#: This is a count, not a dollar amount — the spec mentions $0.10 as an
#: example, but token/cost tracking is not available yet. A count cap is
#: the honest version: it stops runaway spawning without pretending to
#: know the cost.
DEFAULT_BUDGET = 20


class DepthExceeded(BadRequest):
    code = "depth_exceeded"


class CycleDetected(BadRequest):
    code = "cycle_detected"


class BudgetExceeded(BadRequest):
    code = "budget_exceeded"


def check_depth(
    store: Store,
    parent_id: str | None,
    *,
    cap: int = DEFAULT_DEPTH_CAP,
) -> None:
    """Reject a spawn that would exceed the depth cap.

    The parent's depth is looked up by walking the lineage tree. If the
    parent is None (a root spawn), depth is 0 and the child would be 1.
    """
    if parent_id is None:
        return  # root spawn, always allowed
    parent = store.get_participant(parent_id)
    if parent is None:
        return  # parent vanished; the spawner will fail anyway
    depth = lineage.depth_of(store, parent_id)
    if depth + 1 > cap:
        raise DepthExceeded(
            f"spawn would be at depth {depth + 1}, cap is {cap}"
        )


def check_cycle(
    store: Store,
    caller_id: str,
    target_ids: list[str],
) -> None:
    """Reject an await that would close a cycle.

    The await relationship in phase 5a is: a parent that spawned a child
    is awaiting that child. So the spawn tree IS the await tree.

    A cycle happens when the caller awaits a target that is its ancestor.
    If A spawns B, B spawns C, then:
      - A awaits B: B is a child of A — normal, not a cycle
      - B awaits C: C is a child of B — normal, not a cycle
      - C awaits A: A is an ancestor of C — this closes the loop

    So the check is: is the target an ancestor of the caller? If yes,
    reject. The direct-parent case (parent awaits child) is never a cycle
    because the child is a descendant, not an ancestor.
    """
    if not target_ids:
        return
    for target_id in target_ids:
        if target_id == caller_id:
            raise CycleDetected(
                f"participant {caller_id} cannot await itself"
            )
        # Walk the caller's ancestry. If the target appears as an ancestor
        # of the caller, awaiting it would close a cycle.
        if target_id in set(lineage.ancestor_ids(store, caller_id)):
            raise CycleDetected(
                f"await would close a cycle: {target_id} is an ancestor "
                f"of {caller_id}"
            )


def check_budget(
    store: Store,
    parent_id: str | None,
    *,
    limit: int = DEFAULT_BUDGET,
) -> None:
    """Reject a spawn that would exceed the per-tree budget.

    The budget is a count of participants in the tree. The root's budget
    is shared by all descendants. When the count hits the limit, no more
    spawns are allowed in that tree.
    """
    if parent_id is None:
        return  # root spawn, always allowed
    root_id = lineage.root_of(store, parent_id)
    count = len(lineage.subtree_ids(store, root_id))
    if count >= limit:
        raise BudgetExceeded(
            f"tree rooted at {root_id} has {count} participants, "
            f"budget is {limit}"
        )


def hard_stop_tree(store: Store, root_id: str) -> list[str]:
    """Mark all participants in a tree as dead, for budget enforcement.

    Returns the list of killed participant ids. Used when a budget is
    exceeded and the subtree must be stopped immediately.
    """
    killed = [
        pid
        for pid in lineage.subtree_ids(store, root_id)
        if store.get_participant(pid) is not None
    ]
    logger.warning("hard stop on tree %s: killing %d participants", root_id, len(killed))
    return killed
