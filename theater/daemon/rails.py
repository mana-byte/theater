"""Safety rails: depth cap, await-cycle detection, per-tree budget, model allowlist.
The budget refuses new spawns but never kills running work; the allowlist bounds per-turn spend,
which a count cannot see.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from theater.config import RailsSection
from theater.daemon import lineage
from theater.daemon.store import Store
from theater.models import BadRequest, Status

logger = logging.getLogger("theater.rails")

#: Fallbacks (config-owned); depth 3 = root→child→child→child; budget = participant count, not $.
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


class ReasoningNotAllowed(BadRequest):
    code = "reasoning_not_allowed"


def check_model_allowed(harness: str, model: str | None, allowed: list[str]) -> None:
    """Reject a spawn naming a model the config does not list for this harness.
    Empty (the default) refuses only an explicit ``--model``, never a plain spawn; membership is
    about the user's spending intent, not whether the name is real.
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


def check_reasoning_allowed(harness: str, reasoning_effort: str | None, allowed: list[str]) -> None:
    """Reject a spawn naming a reasoning effort the config does not list; same policy as models.

    ``""`` is refused: it would pass the allowlist and then be silently dropped by the adapter.
    """
    if reasoning_effort is None:
        return
    if not reasoning_effort:
        raise ReasoningNotAllowed(
            "reasoning effort must not be empty: omit --reasoning-effort to use the harness default"
        )
    if not allowed:
        raise ReasoningNotAllowed(
            f"no reasoning efforts are configured for harness {harness!r}, so "
            f"--reasoning-effort cannot be used with it: add them under "
            f"[reasoning] in the config file, or omit --reasoning-effort to "
            f"use that CLI's own default"
        )
    if reasoning_effort not in allowed:
        raise ReasoningNotAllowed(
            f"reasoning effort {reasoning_effort!r} is not configured for "
            f"harness {harness!r}: allowed are {', '.join(sorted(allowed))}"
        )


def check_depth(
    store: Store,
    parent_id: str | None,
    *,
    cap: int = DEFAULT_DEPTH_CAP,
) -> None:
    """Reject a spawn that would exceed the depth cap (a root spawn's child is depth 1)."""
    if parent_id is None:
        return
    parent = store.get_participant(parent_id)
    if parent is None:
        return  # parent vanished; the spawner will fail anyway
    depth = lineage.depth_of(store, parent_id)
    if depth + 1 > cap:
        raise DepthExceeded(f"spawn would be at depth {depth + 1}, cap is {cap}")


def check_cycle(
    store: Store,
    caller_id: str,
    target_ids: list[str],
) -> None:
    """Reject an await that would close a cycle: the target is an ancestor of the caller.

    Lineage approximation; catches a descendant blocking on an ancestor before that ancestor awaits.
    """
    if not target_ids:
        return
    for target_id in target_ids:
        if target_id == caller_id:
            raise CycleDetected(f"participant {caller_id} cannot await itself")
        # Walk the caller's ancestry; if the target is an ancestor, awaiting it would close a cycle.
        if target_id in set(lineage.ancestor_ids(store, caller_id)):
            raise CycleDetected(
                f"await would close a cycle: {target_id} is an ancestor of {caller_id}"
            )


def check_wait_cycle(
    graph: Mapping[str, set[str]],
    caller_id: str,
    target_ids: list[str],
) -> None:
    """Reject an await that would close a loop in the live wait graph.
    Catches unrelated peers ``check_cycle`` cannot; every edge is a call in flight, so the loop is
    real. Registration and teardown are synchronous, so no await point separates check from edge.
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
                    f"await would deadlock: {target_id} is already waiting on {caller_id}"
                )
            frontier.extend(graph.get(node, ()))


def check_budget(
    store: Store,
    parent_id: str | None,
    *,
    limit: int = DEFAULT_BUDGET,
) -> None:
    """Reject a spawn that would exceed the per-tree budget of live participants.

    Dead retained rows release their allowance.
    """
    if parent_id is None:
        return
    root_id = lineage.root_of(store, parent_id)
    count = sum(
        participant.status is not Status.DEAD for participant in lineage.subtree(store, root_id)
    )
    if count >= limit:
        raise BudgetExceeded(
            f"tree rooted at {root_id} has {count} live participants, budget is {limit}"
        )
