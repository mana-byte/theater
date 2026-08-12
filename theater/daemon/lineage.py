"""One way to walk the lineage tree.

The parent edge was being walked in five places — the depth cap, the budget,
the cycle check, and the registry's own two accessors — each with its own
hand-rolled visited set. That is four chances to forget the visited set, and a
lineage cycle is not hypothetical: `parent_id` is a plain column that nothing
stops from pointing back up its own chain, and the rails that would catch such
a thing are themselves built on these walks. A rail must not be the unsafe part.

So every walk here terminates on a repeat, and none of them raise: a caller
asking about a participant that no longer exists gets an empty answer, not an
exception. Interpreting that emptiness is the caller's job, because the right
reaction differs — the registry raises, the rails wave the spawn through.
"""

from __future__ import annotations

from collections.abc import Iterator

from theater.daemon.store import Store


def ancestor_ids(store: Store, pid: str) -> Iterator[str]:
    """Parent, grandparent, and so on upward. Stops at the first repeat.

    Yields the id recorded on the child even when no such row exists. A
    dangling link is still a level of lineage, and skipping it would let one
    broken row understate a participant's depth and slip it past the cap.
    """
    seen = {pid}
    current = store.get_participant(pid)
    while current is not None and current.parent_id:
        if current.parent_id in seen:
            return
        seen.add(current.parent_id)
        yield current.parent_id
        current = store.get_participant(current.parent_id)


def depth_of(store: Store, pid: str) -> int:
    """Distance from the root of the lineage. Roots are 0."""
    return sum(1 for _ in ancestor_ids(store, pid))


def root_of(store: Store, pid: str) -> str:
    """The topmost participant that actually exists. Itself, if it is a root.

    A participant we have never heard of is its own root; the caller decides
    whether that is worth complaining about.
    """
    if store.get_participant(pid) is None:
        return pid
    root = pid
    for ancestor_id in ancestor_ids(store, pid):
        if store.get_participant(ancestor_id) is None:
            break
        root = ancestor_id
    return root


def subtree_ids(store: Store, root_id: str) -> list[str]:
    """The root and everything under it, breadth-first, each id once."""
    found: list[str] = []
    seen: set[str] = set()
    queue = [root_id]
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        found.append(pid)
        queue.extend(child.id for child in store.children_of(pid))
    return found
