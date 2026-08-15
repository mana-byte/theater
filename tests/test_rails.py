"""Tests for safety rails: depth cap, cycle detection, budget.

The exit criteria from the plan:
  - deliberately construct A→await B→await A and get a clean rejection
  - set a budget and watch a subtree stop
"""

from __future__ import annotations

import pytest

from theater.daemon.rails import (
    BudgetExceeded,
    CycleDetected,
    DepthExceeded,
    ModelNotAllowed,
    check_budget,
    check_cycle,
    check_depth,
    check_model_allowed,
    check_wait_cycle,
)
from theater.daemon.registry import Registry

# ---- depth cap ----------------------------------------------------------


def test_root_spawn_is_always_allowed(store):
    """A spawn with no parent is a root; depth 0, always allowed."""
    check_depth(store, None)  # does not raise


def test_depth_at_cap_is_allowed(store):
    """Depth exactly at the cap is allowed; cap+1 is not."""
    reg = Registry(store)
    root = reg.create_spawned(harness="vibe", cwd="/tmp")
    d1 = reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=root.id)
    d2 = reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=d1.id)
    # d2 is depth 2; spawning from d2 would be depth 3, which equals the cap
    check_depth(store, d2.id)  # does not raise


def test_depth_exceeding_cap_is_rejected(store):
    reg = Registry(store)
    root = reg.create_spawned(harness="vibe", cwd="/tmp")
    d1 = reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=root.id)
    d2 = reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=d1.id)
    d3 = reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=d2.id)
    # d3 is depth 3; spawning from d3 would be depth 4, exceeding the cap of 3
    with pytest.raises(DepthExceeded):
        check_depth(store, d3.id)


def test_custom_cap_is_respected(store):
    reg = Registry(store)
    root = reg.create_spawned(harness="vibe", cwd="/tmp")
    # With cap=1, spawning a child (depth 1) is allowed, but a grandchild (depth 2) is not
    check_depth(store, root.id, cap=1)  # does not raise
    child = reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=root.id)
    with pytest.raises(DepthExceeded):
        check_depth(store, child.id, cap=1)


# ---- cycle detection ----------------------------------------------------


def test_await_self_is_rejected(store):
    with pytest.raises(CycleDetected):
        check_cycle(store, "abc", ["abc"])


def test_await_child_is_not_a_cycle(store):
    """A parent awaiting its own child is normal, not a cycle."""
    reg = Registry(store)
    parent = reg.create_spawned(harness="vibe", cwd="/tmp")
    child = reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=parent.id)
    # parent awaits child — this is the normal spawn→await pattern
    check_cycle(store, parent.id, [child.id])  # does not raise


def test_await_that_closes_a_cycle_is_rejected(store):
    """A spawns B, B spawns C. C awaiting A is a cycle.

    The await chain: A awaits B (normal), B awaits C (normal).
    If C awaits A, that closes the loop: A→B→C→A.
    """
    reg = Registry(store)
    a = reg.create_spawned(harness="vibe", cwd="/tmp")
    b = reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=a.id)
    c = reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=b.id)
    # A awaits B: normal (direct parent)
    check_cycle(store, a.id, [b.id])  # does not raise
    # B awaits C: normal (direct parent)
    check_cycle(store, b.id, [c.id])  # does not raise
    # C awaits A: A is in C's ancestry beyond the direct parent → cycle
    with pytest.raises(CycleDetected):
        check_cycle(store, c.id, [a.id])


def test_await_empty_targets_is_ok(store):
    check_cycle(store, "abc", [])  # does not raise


# ---- cycles in the live wait graph --------------------------------------
#
# The lineage check cannot see these: peers share no ancestry, so there is
# nothing to walk. What makes them a deadlock is that both awaits are calls
# in flight right now.


def test_two_peers_awaiting_each_other_is_refused():
    """B is blocked on A. A must not now block on B."""
    with pytest.raises(CycleDetected):
        check_wait_cycle({"b": {"a"}}, "a", ["b"])


def test_awaiting_someone_who_is_waiting_on_a_third_party_is_fine():
    """B is busy waiting on C. A waiting on B still terminates."""
    check_wait_cycle({"b": {"c"}}, "a", ["b"])  # does not raise


def test_a_longer_loop_is_still_a_loop():
    """A -> B -> C -> A. The walk has to follow the whole chain."""
    with pytest.raises(CycleDetected):
        check_wait_cycle({"b": {"c"}, "c": {"a"}}, "a", ["b"])


def test_awaiting_yourself_is_refused():
    with pytest.raises(CycleDetected):
        check_wait_cycle({}, "a", ["a"])


# ---- budget -------------------------------------------------------------


def test_root_spawn_is_not_budget_limited(store):
    check_budget(store, None)  # does not raise


def test_budget_within_limit_is_allowed(store):
    reg = Registry(store)
    root = reg.create_spawned(harness="vibe", cwd="/tmp")
    for _i in range(5):
        reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=root.id)
    # 6 participants (root + 5 children), budget default is 20
    check_budget(store, root.id)  # does not raise


def test_budget_exceeded_rejected(store):
    reg = Registry(store)
    root = reg.create_spawned(harness="vibe", cwd="/tmp")
    for _i in range(3):
        reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=root.id)
    # 4 participants, budget 3 → exceeded
    with pytest.raises(BudgetExceeded):
        check_budget(store, root.id, limit=3)


def test_budget_counts_entire_subtree(store):
    """Budget counts all descendants, not just direct children."""
    reg = Registry(store)
    root = reg.create_spawned(harness="vibe", cwd="/tmp")
    child = reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=root.id)
    reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=child.id)
    # 3 participants. Budget 3 means the tree is full.
    with pytest.raises(BudgetExceeded):
        check_budget(store, child.id, limit=3)


# ---- the model allowlist ------------------------------------------------


def test_naming_no_model_is_always_allowed():
    """The case every install starts in, and the one that must never break."""
    check_model_allowed("vibe", None, [])  # does not raise


def test_a_listed_model_is_allowed():
    check_model_allowed("vibe", "big", ["small", "big"])  # does not raise


def test_a_model_that_is_not_listed_is_refused():
    with pytest.raises(ModelNotAllowed) as exc:
        check_model_allowed("vibe", "enormous", ["small", "big"])
    message = str(exc.value)
    assert "enormous" in message
    # The allowed set, so the fix does not need a second command.
    assert "big, small" in message


def test_an_empty_allowlist_refuses_an_explicit_model():
    """Not silently coerced to the default: that starts the wrong model."""
    with pytest.raises(ModelNotAllowed) as exc:
        check_model_allowed("vibe", "big", [])
    assert "theater models --discover vibe" in str(exc.value)


def test_the_empty_case_names_the_harness_it_refused_for():
    with pytest.raises(ModelNotAllowed) as exc:
        check_model_allowed("claude", "big", [])
    assert "claude" in str(exc.value)


def test_matching_is_exact():
    """No prefixes, no case folding: the CLI's spelling is the only spelling."""
    with pytest.raises(ModelNotAllowed):
        check_model_allowed("vibe", "BIG", ["big"])
    with pytest.raises(ModelNotAllowed):
        check_model_allowed("vibe", "big", ["bigger"])
