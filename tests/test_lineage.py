"""Walking the parent edge.

These walks sit underneath the safety rails, so the interesting cases are the
broken ones: a lineage that loops, a parent link pointing at nothing. Neither
may hang, and neither may quietly report a shallower tree than exists — that
is how a runaway spawn gets past the depth cap.
"""

from __future__ import annotations

from theater.daemon.lineage import ancestor_ids, depth_of, root_of, subtree, subtree_ids
from theater.daemon.registry import Registry
from theater.models import Status


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


# ---- ancestors ----------------------------------------------------------


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
    orphan.parent_id = "ghost"
    store.upsert_participant(orphan)
    assert list(ancestor_ids(store, orphan.id)) == ["ghost"]
    assert depth_of(store, orphan.id) == 1


# ---- cycles -------------------------------------------------------------


def test_a_two_node_loop_terminates(store):
    a, b = chain(store, 2)
    a.parent_id = b.id  # a -> b -> a
    store.upsert_participant(a)
    assert depth_of(store, b.id) <= 2
    assert root_of(store, b.id) in {a.id, b.id}


def test_a_participant_that_parents_itself_terminates(store):
    (a,) = chain(store, 1)
    a.parent_id = a.id
    store.upsert_participant(a)
    assert list(ancestor_ids(store, a.id)) == []
    assert depth_of(store, a.id) == 0
    assert root_of(store, a.id) == a.id


# ---- roots --------------------------------------------------------------


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
    child.parent_id = "ghost"
    store.upsert_participant(child)
    assert root_of(store, child.id) == child.id


# ---- subtrees -----------------------------------------------------------


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
