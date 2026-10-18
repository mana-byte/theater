"""Registering participants, walking the parent edge, and the spawn rails built on it."""

from __future__ import annotations

import pytest

from theater.daemon.lineage import ancestor_ids, depth_of, root_of, subtree, subtree_ids
from theater.daemon.rails import (
    BudgetExceeded,
    CycleDetected,
    DepthExceeded,
    ModelNotAllowed,
    ReasoningNotAllowed,
    check_budget,
    check_cycle,
    check_depth,
    check_model_allowed,
    check_reasoning_allowed,
    check_wait_cycle,
)
from theater.daemon.registry import Registry
from theater.models import BadRequest, NotFound, Status, Tier


def test_pane_only_registration_is_refused(registry):
    with pytest.raises(BadRequest, match="provider terminal adoption"):
        registry.register(harness="vibe", pane="%3", cwd="/tmp")


def test_no_pane_makes_you_external_and_unreachable(registry):
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    assert p.tier is Tier.EXTERNAL
    assert not p.addressable


def test_spawned_identity_survives_hello(registry):
    created = registry.create_spawned(harness="vibe", cwd="/tmp")

    # The MCP server comes up later and presents the id it was given on argv.
    seen = registry.register(harness="vibe", pane=None, cwd="/tmp", claimed_id=created.id)
    assert seen.id == created.id
    assert seen.tier is Tier.SPAWNED
    assert seen.tmux_pane is None
    assert seen.status is Status.IDLE


def test_an_unknown_claimed_id_is_honoured_not_refused(registry):
    """A daemon restart must not orphan a live agent."""
    p = registry.register(harness="vibe", pane=None, cwd="/tmp", claimed_id="ghost123")
    assert p.id == "ghost123"
    assert p.tier is Tier.EXTERNAL


def test_tree_nests_children(registry):
    root = registry.create_spawned(harness="vibe", cwd="/tmp")
    child = registry.create_spawned(harness="claude", cwd="/tmp", parent_id=root.id)

    tree = registry.tree()
    assert len(tree) == 1
    assert tree[0]["id"] == root.id
    assert [c["id"] for c in tree[0]["children"]] == [child.id]


def test_get_missing_raises(registry):
    with pytest.raises(NotFound):
        registry.get("nope")


def test_resolve_finds_by_id(registry):
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    found = registry.resolve(p.id)
    assert found.id == p.id


def test_resolve_finds_by_exact_name(registry):
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    found = registry.resolve(p.name)
    assert found.id == p.id


def test_resolve_finds_by_differently_cased_name(registry):
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    found = registry.resolve(p.name.upper())
    assert found.id == p.id


def test_resolve_raises_for_a_stranger(registry):
    with pytest.raises(NotFound):
        registry.resolve("nobody")


def test_resolve_returns_participant_with_name(registry):
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    found = registry.resolve(p.id)
    assert found.name is not None


def chain(store, length: int):
    """A straight line of participants, root first."""
    reg = Registry(store)
    people = []
    parent_id = None
    for _ in range(length):
        p = reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=parent_id)
        people.append(p)
        parent_id = p.id
    return people


def test_a_root_has_no_ancestors(store):
    (root,) = chain(store, 1)
    assert list(ancestor_ids(store, root.id)) == []


def test_ancestors_come_back_nearest_first(store):
    root, child, grandchild = chain(store, 3)
    assert list(ancestor_ids(store, grandchild.id)) == [child.id, root.id]


def test_a_participant_we_never_heard_of_has_no_ancestors(store):
    assert list(ancestor_ids(store, "nobody")) == []


def test_a_dangling_parent_link_still_counts_as_a_level(store):
    """Depth must not shrink because a parent row is missing.

    Understating depth is the failure that matters: it is what lets a spawn
    slip under the cap. Counting a link we cannot follow is the safe error.
    """
    (orphan,) = chain(store, 1)
    store.reparent_participant(orphan.id, new_parent_id="ghost")
    assert list(ancestor_ids(store, orphan.id)) == ["ghost"]
    assert depth_of(store, orphan.id) == 1


def test_a_two_node_loop_terminates(store):
    a, b = chain(store, 2)
    store.reparent_participant(a.id, new_parent_id=b.id)  # a -> b -> a
    assert depth_of(store, b.id) <= 2
    assert root_of(store, b.id) in {a.id, b.id}


def test_a_participant_that_parents_itself_terminates(store):
    (a,) = chain(store, 1)
    store.reparent_participant(a.id, new_parent_id=a.id)
    assert list(ancestor_ids(store, a.id)) == []
    assert depth_of(store, a.id) == 0
    assert root_of(store, a.id) == a.id


def test_depth_counts_hops_to_the_root(store):
    root, child, grandchild = chain(store, 3)
    assert depth_of(store, root.id) == 0
    assert depth_of(store, child.id) == 1
    assert depth_of(store, grandchild.id) == 2


def test_every_node_in_a_chain_shares_a_root(store):
    root, child, grandchild = chain(store, 3)
    assert {root_of(store, p.id) for p in (root, child, grandchild)} == {root.id}


def test_an_unknown_participant_is_its_own_root(store):
    """The bare walk does not raise; the registry is what insists."""
    assert root_of(store, "nobody") == "nobody"


def test_the_root_is_the_last_participant_that_exists(store):
    _root, child = chain(store, 2)
    store.reparent_participant(child.id, new_parent_id="ghost")
    assert root_of(store, child.id) == child.id


def test_a_lone_participant_is_a_subtree_of_one(store):
    (root,) = chain(store, 1)
    assert subtree_ids(store, root.id) == [root.id]


def test_a_subtree_includes_grandchildren(store):
    root, child, grandchild = chain(store, 3)
    assert set(subtree_ids(store, root.id)) == {root.id, child.id, grandchild.id}


def test_a_subtree_is_taken_from_where_you_ask_not_the_root(store):
    _root, child, grandchild = chain(store, 3)
    assert set(subtree_ids(store, child.id)) == {child.id, grandchild.id}


def test_subtree_returns_participants_in_breadth_first_order(store):
    reg = Registry(store)
    root = reg.create_spawned(harness="vibe", cwd="/tmp")
    first = reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=root.id)
    second = reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=root.id)
    grandchild = reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=first.id)

    participants = subtree(store, root.id)

    assert [participant.id for participant in participants] == [
        root.id,
        first.id,
        second.id,
        grandchild.id,
    ]
    assert subtree_ids(store, root.id) == [participant.id for participant in participants]


def test_subtree_crosses_dead_intermediary(store):
    reg = Registry(store)
    root = reg.create_spawned(harness="vibe", cwd="/tmp")
    dead = reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=root.id)
    live = reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=dead.id)
    store.set_status(dead.id, Status.DEAD)

    assert [participant.id for participant in subtree(store, root.id)] == [
        root.id,
        dead.id,
        live.id,
    ]


def test_a_loop_below_the_root_does_not_repeat_anyone(store):
    root, child = chain(store, 2)
    root.parent_id = child.id  # child's child is its own parent
    store.upsert_participant(root)
    ids = subtree_ids(store, root.id)
    assert sorted(ids) == sorted({root.id, child.id})


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
    with pytest.raises(DepthExceeded) as raised:
        check_depth(store, d3.id)
    assert "past depth 3" in raised.value.details["next"]
    assert "do the work yourself" in raised.value.details["next"]


def test_custom_cap_is_respected(store):
    reg = Registry(store)
    root = reg.create_spawned(harness="vibe", cwd="/tmp")
    # With cap=1, spawning a child (depth 1) is allowed, but a grandchild (depth 2) is not
    check_depth(store, root.id, cap=1)  # does not raise
    child = reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=root.id)
    with pytest.raises(DepthExceeded) as raised:
        check_depth(store, child.id, cap=1)
    assert "past depth 1" in raised.value.details["next"]


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
    with pytest.raises(BudgetExceeded) as raised:
        check_budget(store, root.id, limit=3)
    assert "whole tree holds the budget of 3 live participants" in raised.value.details["next"]
    assert "ask the user to retire children whose work is merged" in raised.value.details["next"]
    assert "dead participants free their slot" in raised.value.details["next"]


def test_budget_counts_entire_subtree(store):
    """Budget counts all descendants, not just direct children."""
    reg = Registry(store)
    root = reg.create_spawned(harness="vibe", cwd="/tmp")
    child = reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=root.id)
    reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=child.id)
    # 3 participants. Budget 3 means the tree is full.
    with pytest.raises(BudgetExceeded):
        check_budget(store, child.id, limit=3)


def test_dead_leaf_releases_budget(store):
    reg = Registry(store)
    root = reg.create_spawned(harness="vibe", cwd="/tmp")
    dead = reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=root.id)
    store.set_status(dead.id, Status.DEAD)

    check_budget(store, root.id, limit=2)


def test_dead_intermediary_does_not_hide_live_descendant_from_budget(store):
    reg = Registry(store)
    root = reg.create_spawned(harness="vibe", cwd="/tmp")
    dead = reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=root.id)
    live = reg.create_spawned(harness="vibe", cwd="/tmp", parent_id=dead.id)
    store.set_status(dead.id, Status.DEAD)

    with pytest.raises(BudgetExceeded, match="2 live participants"):
        check_budget(store, root.id, limit=2)

    assert live.status is Status.IDLE


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


def test_naming_no_reasoning_effort_is_always_allowed():
    """The case every install starts in, and the one that must never break."""
    check_reasoning_allowed("codex", None, [])  # does not raise


def test_a_listed_reasoning_effort_is_allowed():
    check_reasoning_allowed("codex", "high", ["low", "high"])  # does not raise


def test_a_reasoning_effort_that_is_not_listed_is_refused():
    with pytest.raises(ReasoningNotAllowed) as exc:
        check_reasoning_allowed("codex", "ultra", ["low", "high"])
    message = str(exc.value)
    assert "ultra" in message
    assert "high, low" in message


def test_an_empty_reasoning_allowlist_refuses_an_explicit_effort():
    with pytest.raises(ReasoningNotAllowed) as exc:
        check_reasoning_allowed("codex", "high", [])
    assert "no reasoning efforts are configured" in str(exc.value)


def test_the_reasoning_empty_case_names_the_harness_it_refused_for():
    with pytest.raises(ReasoningNotAllowed) as exc:
        check_reasoning_allowed("claude", "high", [])
    assert "claude" in str(exc.value)


def test_an_empty_string_reasoning_effort_is_rejected():
    """None means "use default", but "" is a non-value that would be silently
    dropped by the adapter's truthiness guard."""
    with pytest.raises(ReasoningNotAllowed) as exc:
        check_reasoning_allowed("codex", "", ["low", "high", ""])
    assert "must not be empty" in str(exc.value)


def test_reasoning_matching_is_exact():
    with pytest.raises(ReasoningNotAllowed):
        check_reasoning_allowed("codex", "HIGH", ["high"])
    with pytest.raises(ReasoningNotAllowed):
        check_reasoning_allowed("codex", "high", ["higher"])
