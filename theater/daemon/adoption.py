"""Adopt a live sibling as a direct child: lineage plus ownership in one write.

Every control gate keyed on "control_owner_id or parent_id" moves with the target.
"""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy import Connection

from theater.daemon import lineage
from theater.daemon.control_ownership import (
    OwnershipConflict,
    _commit_owner_change,
    _owner_id,
    _reject_cycles,
)
from theater.daemon.rails import BudgetExceeded, CycleDetected, DepthExceeded
from theater.harness.contracts.runtime import RuntimeCapability
from theater.models import (
    Busy,
    ControlOwnerKind,
    DeadTarget,
    NotAddressable,
    NotFound,
    NotSibling,
    Participant,
    SelfAdopt,
    Status,
    now,
)


def _subtree_height(store, root_id: str) -> int:
    """Generations below ``root_id``; 0 for a leaf. Stops on a repeat, like lineage."""
    height = 0
    seen = {root_id}
    frontier = [root_id]
    while frontier:
        children: list[str] = []
        for pid in frontier:
            for child in store.children_of(pid):
                if child.id not in seen:
                    seen.add(child.id)
                    children.append(child.id)
        if not children:
            return height
        height += 1
        frontier = children
    return height


def _live_subtree_count(store, root_id: str) -> int:
    return sum(p.status is not Status.DEAD for p in lineage.subtree(store, root_id))


class AdoptionService:
    """Eligibility plus one atomic reparent-and-reown write."""

    def __init__(self, daemon) -> None:
        self._daemon = daemon

    async def adopt(self, target_token: str, caller_token: str) -> Participant:
        """Reparent ``target_token`` under ``caller_token`` and reown it to the caller."""
        caller = self._daemon.registry.resolve(caller_token)
        target = self._daemon.registry.resolve(target_token)
        async with self._daemon.controls.hold_participant_locks([caller.id, target.id]):
            with self._daemon.store.write_unit() as unit:
                fresh_caller = self._fresh(unit.connection, caller.id)
                fresh_target = self._fresh(unit.connection, target.id)
                self._check(fresh_caller, fresh_target, connection=unit.connection)
                updated = replace(
                    fresh_target,
                    parent_id=fresh_caller.id,
                    control_owner_kind=ControlOwnerKind.PARTICIPANT,
                    control_owner_id=fresh_caller.id,
                    control_revision=fresh_target.control_revision + 1,
                )
                self._daemon.store.reparent_in_connection(
                    fresh_target.id,
                    new_parent_id=fresh_caller.id,
                    connection=unit.connection,
                )
                _commit_owner_change(
                    self._daemon.store,
                    self._daemon.registry,
                    self._daemon.controls,
                    [updated],
                    [fresh_target.id],
                    unit=unit,
                    timestamp=now(),
                )
        # The DB row has no live alias; the registry view carries the participant's name.
        return self._daemon.registry.get(updated.id)

    def _fresh(self, connection: Connection, pid: str) -> Participant:
        participant = self._daemon.store.get_participant(pid, connection=connection)
        if participant is None:
            raise NotFound(
                f"no participant {pid!r}: it died or was cleaned up while adoption was "
                "starting; list_participants and pick a live target"
            )
        return participant

    def _check(self, caller: Participant, target: Participant, *, connection: Connection) -> None:
        """Apply the eligibility rules in refusal order, on rows read inside the unit."""
        if target.id == caller.id:
            raise SelfAdopt(
                f"refusing to adopt {target.id!r}: that is you, not a sibling; adopt a "
                "fellow child of your parent, or a fellow root when you are a root"
            )
        if target.status is Status.DEAD:
            raise DeadTarget(
                f"cannot adopt {target.id!r}: it is dead, and only a live participant can "
                "be adopted; resume it instead if its session is resumable"
            )
        if caller.status is Status.DEAD:
            raise OwnershipConflict(
                f"cannot adopt {target.id!r}: the caller {caller.id!r} is not live, and a "
                "dead participant cannot take ownership — resume the caller first"
            )
        for pid in (target.id, caller.id):
            if self._daemon.store.operations.has_unsettled_terminate(pid, connection=connection):
                raise Busy(
                    f"cannot adopt {target.id!r}: a termination for {pid!r} is accepted "
                    "and still unsettled, and adoption must not reown a participant "
                    "mid-kill; wait for the termination to settle, then adopt from whoever "
                    "owns the target"
                )
        route = self._daemon.controls.route_for(
            target.id, RuntimeCapability.SEND, connection=connection
        )
        if not route.route_available:
            raise NotAddressable(
                f"cannot adopt {target.id!r}: it has no verified terminal-provider or "
                "native-runtime route, so it could not be controlled after adoption; wait "
                "for a provider binding or resume it instead"
            )
        if caller.parent_id != target.parent_id:
            raise NotSibling(
                f"cannot adopt {target.id!r}: its parent is {target.parent_id!r} while "
                f"yours is {caller.parent_id!r}; only siblings — or two roots — can be "
                "adopted"
            )
        self._check_owner_is_the_shared_parent(caller, target)
        self._check_rails(caller, target)
        if target.id in set(lineage.ancestor_ids(self._daemon.store, caller.id)):
            raise CycleDetected(
                f"refusing to adopt {target.id!r}: it is an ancestor of {caller.id!r}; "
                "the lineage is inconsistent, so report the cycle rather than repairing "
                "it by adoption"
            )
        proposed = {
            participant.id: _owner_id(participant)
            for participant in self._daemon.store.list_participants(
                include_dead=True, connection=connection
            )
        }
        proposed[target.id] = caller.id
        _reject_cycles(proposed, [target.id], operation="adoption")

    def _check_owner_is_the_shared_parent(self, caller: Participant, target: Participant) -> None:
        owner_kind = target.control_owner_kind or (
            ControlOwnerKind.PARTICIPANT
            if target.parent_id is not None
            else ControlOwnerKind.LOCAL_OPERATOR
        )
        owner_id = target.control_owner_id or target.parent_id
        if caller.parent_id is None:
            if owner_kind is not ControlOwnerKind.LOCAL_OPERATOR:
                raise OwnershipConflict(
                    f"cannot adopt {target.id!r}: its current control owner is "
                    f"{owner_id or owner_kind.value!r}, and a root may be adopted only "
                    "while the local operator owns it; have that owner release control first"
                )
            return
        if owner_kind is not ControlOwnerKind.PARTICIPANT or owner_id != caller.parent_id:
            raise OwnershipConflict(
                f"cannot adopt {target.id!r}: its current control owner is "
                f"{owner_id or owner_kind.value!r}, not its parent {caller.parent_id!r}; "
                "adoption must not steal control a third party was handed — have that "
                "owner release it first (transfer_control)"
            )

    def _check_rails(self, caller: Participant, target: Participant) -> None:
        rails = self._daemon.config.rails
        deepest = (
            lineage.depth_of(self._daemon.store, caller.id)
            + 1
            + _subtree_height(self._daemon.store, target.id)
        )
        if deepest > rails.depth_cap:
            raise DepthExceeded(
                f"adoption would place the deepest node of {target.id!r}'s subtree at "
                f"depth {deepest}, over the cap of {rails.depth_cap}; adopt a shallower "
                "sibling, or raise [rails] depth_cap in the config"
            )
        if caller.parent_id is None:
            merged = _live_subtree_count(self._daemon.store, caller.id) + _live_subtree_count(
                self._daemon.store, target.id
            )
            if merged > rails.budget:
                raise BudgetExceeded(
                    f"adoption would merge the two trees into {merged} live participants, "
                    f"over the budget of {rails.budget}; retire participants first or raise "
                    "[rails] budget in the config"
                )


__all__ = ["AdoptionService"]
