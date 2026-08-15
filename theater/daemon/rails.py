"""Safety rails: depth cap, cycle detection, per-tree budget, model allowlist.

These are the guardrails that make multi-agent orchestration safe enough
to leave running unattended. Without them, a runaway agent could spawn a
deep subtree that exhausts the machine, or close an await cycle that
deadlocks the daemon.

Four rails:

1. **Depth cap** (default 3). Enforced at `spawn`: the depth of the new
   child in the lineage tree must not exceed the cap. Wide fan-out is
   often correct; deep recursion rarely is. The cap is configurable per
   tree root, so a human can raise it for a specific task.

2. **Cycle detection** on the await graph. If A awaits B and B awaits A,
   both block forever — async killed this deadlock; `await` revived it.
   Two checks, because there are two ways to see the same loop.
   `check_wait_cycle` reads the awaits that are actually in flight and
   rejects one that would close a loop among them: exact, and the only
   thing that catches two peers with no family relation. `check_cycle`
   is the older approximation over lineage, which catches a descendant
   about to block on an ancestor before that ancestor's own await has
   started. Either rejects with `cycle_detected`.

3. **Per-tree budget**. A tree may hold so many participants and no more,
   counted from its root, so a runaway spawner exhausts its own allowance
   rather than the machine. Reaching the limit rejects the next `spawn`
   with `budget_exceeded`; it does not stop the participants already
   running. Killing a live subtree on a count alone would destroy work a
   human may be watching, and the régie already offers a kill key. The
   backstop is that nothing new starts.

4. **Model allowlist**. A spawn may only name a model the user listed for
   that harness under `[models]`. The other three rails bound how much a
   tree may spawn; this one bounds what it may spend per turn, which is the
   axis a count cannot see — one child on a frontier model can cost more
   than twenty on a small one. Unset by default, and an unset list refuses
   only an explicit `--model`, never a plain spawn.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from theater.config import RailsSection
from theater.daemon import lineage
from theater.daemon.store import Store
from theater.models import BadRequest

logger = logging.getLogger("theater.rails")

#: Fallbacks for direct calls. `config.RailsSection` owns both literals so
#: the settable value and the default cannot drift; `methods.spawn` passes
#: the configured numbers in.
#:
#: Depth: roots are depth 0, children depth 1. A cap of 3 means a root can
#: spawn children that spawn children that spawn children, but those
#: grandchildren cannot spawn further.
#:
#: Budget: a count of participants in a tree, not a dollar amount. A count
#: cap stops runaway spawning without pretending to know the cost.
_DEFAULTS = RailsSection()

DEFAULT_DEPTH_CAP = _DEFAULTS.depth_cap
DEFAULT_BUDGET = _DEFAULTS.budget


class DepthExceeded(BadRequest):
    code = "depth_exceeded"


class CycleDetected(BadRequest):
    code = "cycle_detected"


class BudgetExceeded(BadRequest):
    code = "budget_exceeded"


class ModelNotAllowed(BadRequest):
    code = "model_not_allowed"


def check_model_allowed(harness: str, model: str | None, allowed: list[str]) -> None:
    """Reject a spawn naming a model the config does not list for this harness.

    `allowed` is `[models].<harness>` from the user's config. Empty — which is
    the default, and true of every install until someone writes the section —
    means no model may be *named*, not that no model runs: omitting `--model`
    is unaffected and the child comes up on whatever its own CLI is configured
    for. So the empty case only ever refuses a request that was made
    explicitly, and never silently swaps the model out from under one.

    Theater checks membership and nothing else. It does not know whether a
    listed name is real, only whether the user vouched for it; the CLI still
    has the last word in the pane. An allowlist here is about intent — which
    models this machine is willing to spend on unattended — not correctness.
    """
    if model is None:
        return
    if not allowed:
        raise ModelNotAllowed(
            f"no models are configured for harness {harness!r}, so --model "
            f"cannot be used with it: add them under [models] in the config "
            f"file (`theater models --discover {harness}` prints a block to "
            f"paste), or omit --model to use that CLI's own default"
        )
    if model not in allowed:
        raise ModelNotAllowed(
            f"model {model!r} is not configured for harness {harness!r}: "
            f"allowed are {', '.join(sorted(allowed))}"
        )


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
        return
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


def check_wait_cycle(
    graph: Mapping[str, set[str]],
    caller_id: str,
    target_ids: list[str],
) -> None:
    """Reject an await that would close a loop in the live wait graph.

    `graph` maps a participant to the participants it is *currently* blocked
    on. Adding caller -> target is a deadlock exactly when target can already
    reach caller by following those edges. Two peers awaiting each other is
    the case `check_cycle` cannot see: they are siblings, or unrelated
    entirely, so there is no ancestry to walk.

    Both parties are blocked inside an MCP tool call, so neither can answer
    the other and neither can notice. They come back when their timeouts
    expire, minutes later, having learned nothing. Refusing the second await
    outright is worse than useless only if the loop was imaginary — and it
    cannot be, because every edge here is a call currently in flight.

    Registration happens before the wait begins and is torn down after it
    ends, both synchronously, so no await point separates the check from the
    edge it is checking.
    """
    for target_id in target_ids:
        if target_id == caller_id:
            raise CycleDetected(f"participant {caller_id} cannot await itself")
        seen: set[str] = set()
        frontier = [target_id]
        while frontier:
            node = frontier.pop()
            if node in seen:
                continue
            seen.add(node)
            if node == caller_id:
                raise CycleDetected(
                    f"await would deadlock: {target_id} is already waiting "
                    f"on {caller_id}"
                )
            frontier.extend(graph.get(node, ()))


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
        return
    root_id = lineage.root_of(store, parent_id)
    count = len(lineage.subtree_ids(store, root_id))
    if count >= limit:
        raise BudgetExceeded(
            f"tree rooted at {root_id} has {count} participants, "
            f"budget is {limit}"
        )
