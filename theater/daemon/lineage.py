"""One way to walk the lineage tree: every walk stops on a repeat and none raise.
``parent_id`` can form a cycle and the rails rely on these walks; a missing participant yields an
empty answer the caller interprets.
"""

from __future__ import annotations

from collections.abc import Iterator

from theater.daemon.store import Store
from theater.models import Participant


def ancestor_ids(store: Store, pid: str) -> Iterator[str]:
    """Parent, grandparent, and so on upward. Stops at the first repeat.

    Dangling ids are still yielded, so a broken row cannot understate depth and slip past the cap.
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

    An unknown participant is its own root; the caller decides whether to complain.
    """
    if store.get_participant(pid) is None:
        return pid
    root = pid
    for ancestor_id in ancestor_ids(store, pid):
        if store.get_participant(ancestor_id) is None:
            break
        root = ancestor_id
    return root


def subtree(store: Store, root_id: str) -> list[Participant]:
    """The root and everything under it, breadth-first, each participant once."""
    root = store.get_participant(root_id)
    if root is None:
        return []
    found: list[Participant] = []
    seen: set[str] = set()
    queue = [root]
    while queue:
        participant = queue.pop(0)
        if participant.id in seen:
            continue
        seen.add(participant.id)
        found.append(participant)
        queue.extend(store.children_of(participant.id))
    return found


def subtree_ids(store: Store, root_id: str) -> list[str]:
    """The ids in :func:`subtree`, in breadth-first order."""
    return [participant.id for participant in subtree(store, root_id)]
