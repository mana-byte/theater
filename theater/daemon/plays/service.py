"""Play membership: start by load, seat every spawn, assign existing children on delivery."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import replace

from theater.daemon import lineage
from theater.daemon.persistence.repositories.plays import PlayAssignment, PlayMembership
from theater.daemon.plays.models import NO_SEAT, PlayPlan, PlayRefused, SpawnSeat
from theater.harness.contracts.runtime import DeliveryResult
from theater.models import JobState, Status, now
from theater.skills import Play, PlayRules

logger = logging.getLogger("theater.plays")


class PlayService:
    """The one authority over play membership; every check reads a play's pinned rules.

    A membership is active only while its master is not dead, so a master's death ends the
    play for everyone at once without a write.
    """

    def __init__(self, daemon) -> None:
        self.store = daemon.store
        self.store.add_settlement_listener(self._on_settled)
        daemon.jobs.add_terminal_listener(self._on_job_terminal)

    # ---- reads ----------------------------------------------------------------------------

    def membership(self, participant_id: str, *, connection=None) -> PlayMembership | None:
        return self.store.plays.active_membership(participant_id, connection=connection)

    def governing(self, participant_id: str, *, connection=None) -> PlayMembership | None:
        """The nearest live membership among the participant and its ancestors."""
        held = self.membership(participant_id, connection=connection)
        if held is not None:
            return held
        for ancestor_id in lineage.ancestor_ids(self.store, participant_id):
            held = self.membership(ancestor_id, connection=connection)
            if held is not None:
                return held
        return None

    def describe(self, participant_id: str) -> dict[str, str] | None:
        held = self.membership(participant_id)
        if held is None:
            return None
        return {
            "play": held.play,
            "position": held.rules.qualified(held.position),
            "master": held.master_id,
        }

    # ---- start by load --------------------------------------------------------------------

    def start_by_load(self, caller_id: str, play: Play) -> dict[str, str]:
        """Make the loading agent its play's master, once, and never inside a live play."""
        with self.store.write_unit() as unit:
            caller = self.store.get_participant(caller_id, connection=unit.connection)
            if caller is None or caller.status is Status.DEAD:
                return _outcome(play, "not_started", reason="only a live agent can start a play")
            governing = self.governing(caller_id, connection=unit.connection)
            if governing is not None:
                mine = governing.is_master and governing.participant_id == caller_id
                if mine and governing.play == play.name:
                    return _outcome(play, "already_master")
                return _outcome(play, "not_started", reason=_inside(governing, caller_id))
            self.store.plays.start(
                caller_id, play.rules, created_at=now(), connection=unit.connection
            )
        return _outcome(play, "started")

    # ---- spawns ---------------------------------------------------------------------------

    def check_spawn(
        self,
        parent_id: str | None,
        plan: PlayPlan,
        *,
        resume_predecessor_id: str | None = None,
        connection=None,
    ) -> SpawnSeat:
        """Seat a spawn's child, or refuse the spawn with the positions it may use."""
        governing = None if parent_id is None else self.governing(parent_id, connection=connection)
        kept = None
        if resume_predecessor_id is not None:
            kept = self.membership(resume_predecessor_id, connection=connection)
        if kept is not None and not kept.is_master:
            return _resumed_seat(parent_id, governing, kept, plan)
        if governing is None:
            return NO_SEAT if plan.start is None else SpawnSeat(start=plan.start)
        rules = governing.rules
        if plan.start is not None or any(rules.member(name) == rules.master for name in plan.names):
            raise PlayRefused(_inside(governing, parent_id))
        if governing.participant_id != parent_id:
            raise PlayRefused(_unpositioned(parent_id, governing))
        positions, roles, foreign = _members(plan.names, rules)
        if foreign:
            raise PlayRefused(
                f"inside the live {rules.play} play a spawn may attach only {rules.play} "
                f"skills and standalone skills; remove: {', '.join(foreign)}"
            )
        if len(positions) != 1 or positions[0] not in rules.children(governing.position):
            raise PlayRefused(_position_required(parent_id, governing, positions))
        _require_role_fit(rules, roles, positions[0])
        return SpawnSeat(master_id=governing.master_id, position=positions[0])

    def record_spawn(self, seat: SpawnSeat, child_id: str, *, connection) -> None:
        if seat.start is not None:
            self.store.plays.start(child_id, seat.start, created_at=now(), connection=connection)
        elif seat.master_id is not None and seat.position is not None:
            self.store.plays.add_member(
                child_id, seat.master_id, seat.position, created_at=now(), connection=connection
            )

    def check_adoption(self, adopter_id: str, target_id: str, *, connection=None) -> None:
        """Adoption never moves a live play, or a member of one, inside a different live play."""
        governing = self.governing(adopter_id, connection=connection)
        if governing is None:
            return
        for participant_id in lineage.subtree_ids(self.store, target_id):
            held = self.membership(participant_id, connection=connection)
            if held is not None and held.master_id != governing.master_id:
                raise PlayRefused(
                    f"cannot adopt {target_id!r}: {participant_id} holds "
                    f"{held.rules.qualified(held.position)} in the live {held.play} play under "
                    f"master {held.master_id}, and you are inside the live {governing.play} "
                    f"play under master {governing.master_id}; one play cannot sit inside another"
                )

    # ---- assignment by send ---------------------------------------------------------------

    def check_assignment(
        self, sender_id: str, target_id: str, names: Sequence[str]
    ) -> PlayAssignment | None:
        """The position a send would give the target once delivered, else None (plain text)."""
        governing = self.governing(target_id) if names else None
        if governing is None:
            return None
        rules = governing.rules
        positions, roles, _foreign = _members(names, rules)
        held = governing.position if governing.participant_id == target_id else None
        if len(positions) > 1:
            raise PlayRefused("attach at most one position: an agent holds one position")
        position = positions[0] if positions else held
        if position is not None:
            _require_role_fit(rules, roles, position)
        if not positions or positions[0] == held:
            return None
        if held is not None:
            raise PlayRefused(
                f"{target_id} already holds {rules.qualified(held)} in the live {rules.play} "
                f"play; a position stays fixed while master {governing.master_id} lives"
            )
        target = self.store.get_participant(target_id)
        parent_id = None if target is None else target.parent_id
        if parent_id != sender_id:
            raise PlayRefused(
                f"only {target_id}'s direct parent ({parent_id or 'none'}) can give it a "
                f"position in the live {rules.play} play"
            )
        sender = self.membership(sender_id)
        if sender is None or sender.master_id != governing.master_id:
            raise PlayRefused(_unpositioned(sender_id, governing))
        allowed = rules.children(sender.position)
        if positions[0] not in allowed:
            raise PlayRefused(
                f"a {rules.qualified(sender.position)} can give its children only: "
                f"{_qualified(rules, allowed) or 'no position'}"
            )
        return PlayAssignment(
            job_handle="",
            participant_id=target_id,
            master_id=governing.master_id,
            position=positions[0],
            assigned_by=sender_id,
        )

    def holder(self, assignment: PlayAssignment | None) -> Callable[[str, str | None], None] | None:
        """The control ``on_reserved`` callback: hold the assignment before delivery starts."""
        if assignment is None:
            return None

        def hold(_operation_id: str, job_handle: str | None) -> None:
            if job_handle is not None:
                self.track_assignment(assignment, job_handle)

        return hold

    def track_assignment(self, assignment: PlayAssignment, job_handle: str) -> None:
        """Hold the assignment until its message is delivered; apply it now if it already was.

        Idempotent: a send records it at reservation and again once routed.
        """
        if self.membership(assignment.participant_id) is not None:
            return
        pending = replace(assignment, job_handle=job_handle)
        delivered = any(
            operation.delivery_result == DeliveryResult.ACCEPTED
            for operation in self.store.control_operations_for_job(job_handle)
        )
        job = self.store.get_job(job_handle)
        with self.store.write_unit() as unit:
            if delivered or (job is not None and job.state == JobState.DONE):
                self._apply(pending, unit.connection)
            elif job is not None and job.state == JobState.RUNNING:
                self.store.plays.put_assignment(
                    pending, created_at=now(), connection=unit.connection
                )

    def _on_settled(self, operation_id: str, result, connection) -> None:
        # A rejection may be requeued under the same job; only the job's end drops the grant.
        if result != DeliveryResult.ACCEPTED:
            return
        operation = self.store.get_control_operation(operation_id, connection=connection)
        if operation is None or operation.job_handle is None:
            return
        pending = self.store.plays.assignment(operation.job_handle, connection=connection)
        if pending is not None:
            self._apply(pending, connection)

    def _on_job_terminal(self, handle: str) -> None:
        """A finished turn proves an uncertain delivery; any other end drops the grant."""
        pending = self.store.plays.assignment(handle)
        if pending is None:
            return
        job = self.store.get_job(handle)
        with self.store.write_unit() as unit:
            if job is not None and job.state == JobState.DONE:
                self._apply(pending, unit.connection)
            else:
                self.store.plays.delete_assignment(handle, connection=unit.connection)

    def _apply(self, pending: PlayAssignment, connection) -> bool:
        """Revalidate against the live play at delivery; a dead master assigns nothing."""
        self.store.plays.delete_assignment(pending.job_handle, connection=connection)
        sender = self.membership(pending.assigned_by, connection=connection)
        target = self.store.get_participant(pending.participant_id, connection=connection)
        if (
            sender is None
            or sender.master_id != pending.master_id
            or pending.position not in sender.rules.children(sender.position)
            or target is None
            or target.parent_id != pending.assigned_by
            or self.membership(pending.participant_id, connection=connection) is not None
        ):
            logger.info("play position for %s not assigned: no longer valid", pending.job_handle)
            return False
        self.store.plays.add_member(
            pending.participant_id,
            pending.master_id,
            pending.position,
            created_at=now(),
            connection=connection,
        )
        return True


def _outcome(play: Play, state: str, *, reason: str | None = None) -> dict[str, str]:
    outcome = {"play": play.name, "state": state}
    if reason is not None:
        outcome["reason"] = reason
    return outcome


def _members(names: Sequence[str], rules: PlayRules) -> tuple[list[str], list[str], list[str]]:
    """Split attached play members into this play's positions, roles, and anything else."""
    positions: list[str] = []
    roles: list[str] = []
    foreign: list[str] = []
    for name in names:
        member = rules.member(name)
        if member is not None and member != rules.master and member in rules.positions:
            positions.append(member)
        elif member is not None and member in rules.roles:
            roles.append(member)
        else:
            foreign.append(name)
    return positions, roles, foreign


def _require_role_fit(rules: PlayRules, roles: Sequence[str], position: str) -> None:
    if len(roles) > 1:
        raise PlayRefused("attach at most one task role beside the position")
    if roles and position not in rules.roles[roles[0]]:
        fits = _qualified(rules, rules.roles_for(position)) or "none"
        raise PlayRefused(
            f"{rules.qualified(roles[0])} does not fit {rules.qualified(position)}; "
            f"roles that fit: {fits}"
        )


def _resumed_seat(
    parent_id: str | None,
    governing: PlayMembership | None,
    kept: PlayMembership,
    plan: PlayPlan,
) -> SpawnSeat:
    """A resumed member keeps its position while its master lives; nothing re-seats it."""
    rules = kept.rules
    if governing is not None and (
        governing.master_id != kept.master_id
        or governing.participant_id != parent_id
        or kept.position not in rules.children(governing.position)
    ):
        raise PlayRefused(
            f"the resumed session holds {rules.qualified(kept.position)} in the live "
            f"{rules.play} play under master {kept.master_id}; inside a live play only an "
            f"agent that may give that position can resume it"
        )
    positions, roles, _foreign = _members(plan.names, rules)
    if plan.start is not None or any(position != kept.position for position in positions):
        raise PlayRefused(
            f"a resumed member keeps {rules.qualified(kept.position)} while master "
            f"{kept.master_id} lives; attach no other position or master skill"
        )
    _require_role_fit(rules, roles, kept.position)
    return SpawnSeat(master_id=kept.master_id, position=kept.position)


def _qualified(rules: PlayRules, members: Sequence[str]) -> str:
    return ", ".join(rules.qualified(member) for member in members)


def _inside(governing: PlayMembership, participant_id: str | None) -> str:
    where = (
        f"you hold {governing.rules.qualified(governing.position)}"
        if governing.participant_id == participant_id
        else f"{participant_id} is under master {governing.master_id}"
    )
    return (
        f"{where} in the live {governing.play} play; no play can start inside a live play, "
        "and an agent belongs to one live play at a time"
    )


def _unpositioned(participant_id: str | None, governing: PlayMembership) -> str:
    rules = governing.rules
    return (
        f"{participant_id} holds no position in the live {rules.play} play (master "
        f"{governing.master_id}), so it cannot spawn or assign positions until its direct "
        f"parent gives it one with send(target='{participant_id}', "
        f"skills=['{rules.play}/<position>'])"
    )


def _position_required(
    parent_id: str | None, governing: PlayMembership, positions: Sequence[str]
) -> str:
    rules = governing.rules
    allowed = rules.children(governing.position)
    holder = rules.qualified(governing.position)
    if not allowed:
        return (
            f"a {holder} cannot spawn while the {rules.play} play's master "
            f"{governing.master_id} lives"
        )
    fits = "; ".join(
        f"{rules.qualified(position)} takes "
        f"{_qualified(rules, rules.roles_for(position)) or 'no roles'}"
        for position in allowed
    )
    return (
        f"while master {governing.master_id} lives, every spawn by {parent_id} ({holder}) "
        f"attaches exactly one position: {_qualified(rules, allowed)}; got "
        f"{_qualified(rules, positions) or 'none'}. Add at most one fitting task role ({fits}) "
        "and any standalone skills"
    )


__all__ = ["PlayService"]
