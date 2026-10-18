"""Play membership: start by load, seat every spawn, assign existing children on delivery."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace

from theater.daemon import lineage
from theater.daemon.persistence.repositories.plays import PlayAssignment, PlayMembership
from theater.daemon.plays.dependencies import require_plugins
from theater.daemon.plays.models import NO_SEAT, PlayPlan, PlayRefused, SpawnSeat
from theater.harness.contracts.runtime import (
    ControlKind,
    ControlTransport,
    DeliveryResult,
    NativeTurnTerminal,
)
from theater.models import Job, JobState, Status, now
from theater.skills import Play, PlayRules

logger = logging.getLogger("theater.plays")

_PROMPT_KINDS = (ControlKind.SEND, ControlKind.QUEUE_FOLLOWUP)


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

    def _memberships_in(self, root_id: str, *, connection=None) -> Iterator[PlayMembership]:
        """Every live membership in ``root_id``'s subtree, the root included."""
        for participant_id in lineage.subtree_ids(self.store, root_id):
            held = self.membership(participant_id, connection=connection)
            if held is not None:
                yield held

    def describe(self, participant_id: str) -> dict[str, str] | None:
        held = self.membership(participant_id)
        if held is None:
            return None
        return {
            "play": held.play,
            "position": held.rules.qualified(held.position),
            "master": held.master_id,
        }

    def self_view(self, participant_id: str) -> dict[str, object] | None:
        """What an agent may spawn in its live play, from the pinned rules: each child position
        with the task roles that fit it. An unpositioned descendant sees the play over it, marked
        unpositioned, with no options; it still holds no membership."""
        governing = self.governing(participant_id)
        if governing is None:
            return None
        rules = governing.rules
        if governing.participant_id != participant_id:
            return {
                "play": rules.play,
                "position": None,
                "master": governing.master_id,
                "unpositioned": True,
                "spawn_options": [],
                "disposable": [],
            }
        return {
            "play": rules.play,
            "position": rules.qualified(governing.position),
            "master": governing.master_id,
            "spawn_options": [
                {
                    "position": rules.qualified(child),
                    "roles": [rules.qualified(role) for role in rules.roles_for(child)],
                }
                for child in rules.children(governing.position)
            ],
            "disposable": [rules.qualified(position) for position in rules.disposable],
        }

    # ---- start by load --------------------------------------------------------------------

    def start_by_load(self, caller_id: str, play: Play) -> dict[str, str]:
        """Make the loading agent its play's master, once, never inside or above a live play."""
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
            below = next(self._memberships_in(caller_id, connection=unit.connection), None)
            if below is not None:
                return _outcome(play, "not_started", reason=_contains(below))
            require_plugins(
                play.rules, store=self.store, participant_id=caller_id, connection=unit.connection
            )
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
        if kept is not None:
            require_plugins(kept.rules)
        if governing is not None:
            require_plugins(governing.rules)
        if plan.start is not None:
            require_plugins(plan.start)
        if kept is not None and not kept.is_master:
            return _resumed_seat(parent_id, governing, kept, plan)
        if governing is None:
            return _seat_outside_a_play(parent_id, plan)
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
        return SpawnSeat(instance_id=governing.instance_id, position=positions[0])

    def record_spawn(self, seat: SpawnSeat, child_id: str, *, connection) -> None:
        if seat.start is not None:
            self.store.plays.start(child_id, seat.start, created_at=now(), connection=connection)
        elif seat.instance_id is not None and seat.position is not None:
            self.store.plays.add_member(
                child_id, seat.instance_id, seat.position, created_at=now(), connection=connection
            )

    def check_adoption(self, adopter_id: str, target_id: str, *, connection=None) -> None:
        """Adoption never nests live plays, and a positioned target needs an adopter whose own
        position may parent it; an unpositioned target waits for its new parent's position."""
        governing = self.governing(adopter_id, connection=connection)
        if governing is None:
            return
        for held in self._memberships_in(target_id, connection=connection):
            if held.instance_id != governing.instance_id:
                raise PlayRefused(
                    f"cannot adopt {target_id!r}: {held.participant_id} holds "
                    f"{held.rules.qualified(held.position)} in the live {held.play} play under "
                    f"master {held.master_id}, and you are inside the live {governing.play} "
                    f"play under master {governing.master_id}; one play cannot sit inside another"
                )
        target = self.membership(target_id, connection=connection)
        if target is None:
            return
        rules = governing.rules
        if governing.participant_id != adopter_id:
            raise PlayRefused(
                f"cannot adopt {target_id!r}: it holds {rules.qualified(target.position)} in the "
                f"live {rules.play} play, and you hold no position there to parent it"
            )
        allowed = rules.children(governing.position)
        if target.position not in allowed:
            raise PlayRefused(
                f"cannot adopt {target_id!r}: it holds {rules.qualified(target.position)}, and a "
                f"{rules.qualified(governing.position)} may parent only "
                f"{_qualified(rules, allowed) or 'no position'} in the live {rules.play} play"
            )

    # ---- sends carrying play skills -------------------------------------------------------

    def check_send(self, sender_id: str, target_id: str, plan: PlayPlan) -> PlayAssignment | None:
        """Bind a message carrying play skills to the position its target holds or is given.

        Only a live play can seat them; a missing position is assigned once delivered. A message
        without play skills is untouched and returns None.
        """
        if not plan.names:
            return None
        if plan.start is not None:
            master = plan.start.qualified(plan.start.master)
            raise PlayRefused(
                f"a send never starts a play: ask {target_id} to load {master} itself, or attach "
                "it on spawn_session to start a new master"
            )
        governing = self.governing(target_id)
        if governing is None:
            raise PlayRefused(_unseated(f"{target_id} is in no live play", plan))
        rules = governing.rules
        require_plugins(rules, store=self.store, participant_id=target_id)
        positions, roles, foreign = _members(plan.names, rules)
        if foreign:
            raise PlayRefused(
                f"{target_id} is in the live {rules.play} play, which cannot seat "
                f"{', '.join(foreign)}; attach only its positions and roles"
            )
        if len(positions) > 1:
            raise PlayRefused("attach at most one position: an agent holds one position")
        held = governing.position if governing.participant_id == target_id else None
        position = positions[0] if positions else held
        if position is None:
            raise PlayRefused(
                f"{target_id} holds no position in the live {rules.play} play, so a task role "
                "has nothing to fit; its direct parent sends a position with it"
            )
        _require_role_fit(rules, roles, position)
        if held is not None and position != held:
            raise PlayRefused(
                f"{target_id} already holds {rules.qualified(held)} in the live {rules.play} "
                f"play; a position stays fixed while master {governing.master_id} lives"
            )
        if held is None:
            self._require_assigner(sender_id, target_id, governing, position)
        return PlayAssignment(
            job_handle="",
            participant_id=target_id,
            instance_id=governing.instance_id,
            position=position,
            assigned_by=sender_id,
        )

    def _require_assigner(
        self, sender_id: str, target_id: str, governing: PlayMembership, position: str
    ) -> None:
        rules = governing.rules
        target = self.store.get_participant(target_id)
        parent_id = None if target is None else target.parent_id
        if parent_id != sender_id:
            raise PlayRefused(
                f"only {target_id}'s direct parent ({parent_id or 'none'}) can give it a "
                f"position in the live {rules.play} play"
            )
        sender = self.membership(sender_id)
        if sender is None or sender.instance_id != governing.instance_id:
            raise PlayRefused(_unpositioned(sender_id, governing))
        allowed = rules.children(sender.position)
        if position not in allowed:
            raise PlayRefused(
                f"a {rules.qualified(sender.position)} can give its children only: "
                f"{_qualified(rules, allowed) or 'no position'}"
            )

    def holder(self, assignment: PlayAssignment | None) -> Callable[[str, str | None], None] | None:
        """The control ``on_reserved`` callback: hold the binding before delivery starts."""
        if assignment is None:
            return None

        def hold(_operation_id: str, job_handle: str | None) -> None:
            if job_handle is not None:
                self.track_assignment(assignment, job_handle)

        return hold

    def track_assignment(self, assignment: PlayAssignment, job_handle: str) -> None:
        """Hold the binding until its message is delivered; apply it now if it already was.

        Idempotent: a send records it at reservation and again once routed.
        """
        pending = replace(assignment, job_handle=job_handle)
        delivered = any(
            operation.delivery_result == DeliveryResult.ACCEPTED
            for operation in self.store.control_operations_for_job(job_handle)
        )
        job = self.store.get_job(job_handle)
        proven = delivered or self._proven(job)
        with self.store.write_unit() as unit:
            if proven:
                self._apply(pending, unit.connection)
            elif job is not None and job.state == JobState.RUNNING:
                self.store.plays.put_assignment(
                    pending, created_at=now(), connection=unit.connection
                )

    def dispatch_refusal(self, job_handle: str) -> PlayRefused | None:
        """A queued message bound to a play is delivered only while that binding still holds."""
        pending = self.store.plays.assignment(job_handle)
        reason = None if pending is None else self._invalid(pending)
        if reason is None:
            return None
        return PlayRefused(f"{reason}; nothing was delivered, so send again if it still applies")

    def _on_settled(self, operation_id: str, result, connection) -> None:
        # A rejection may be requeued under the same job; only the job's end drops the binding.
        if result != DeliveryResult.ACCEPTED:
            return
        operation = self.store.get_control_operation(operation_id, connection=connection)
        if operation is None or operation.job_handle is None:
            return
        pending = self.store.plays.assignment(operation.job_handle, connection=connection)
        if pending is not None:
            self._apply(pending, connection)

    def _on_job_terminal(self, handle: str) -> None:
        """Only exact turn evidence proves an uncertain delivery; other ends drop the binding."""
        pending = self.store.plays.assignment(handle)
        if pending is None:
            return
        proven = self._proven(self.store.get_job(handle))
        with self.store.write_unit() as unit:
            if proven:
                self._apply(pending, unit.connection)
            else:
                self.store.plays.delete_assignment(handle, connection=unit.connection)

    def _proven(self, job: Job | None) -> bool:
        """The message's own named native turn completed: exact proof it arrived.

        Transcript turn matching and rescue can end a job ``done`` with no error code too, but
        they cannot tell which message a turn answered, so they never grant a position.
        """
        if job is None:
            return False
        for operation in self.store.control_operations_for_job(job.handle):
            if (
                operation.kind not in _PROMPT_KINDS
                or operation.transport is not ControlTransport.NATIVE_RUNTIME
                or operation.backend_generation is None
                or operation.native_session_id is None
                or operation.native_turn_id is None
            ):
                continue
            evidence = self.store.get_native_terminal_evidence(
                participant_id=operation.participant_id,
                backend_generation=operation.backend_generation,
                native_session_id=operation.native_session_id,
                native_turn_id=operation.native_turn_id,
            )
            if evidence is not None and evidence.terminal == NativeTurnTerminal.COMPLETED:
                return True
        return False

    def _apply(self, pending: PlayAssignment, connection) -> bool:
        """Revalidate against the live play at delivery; a dead master assigns nothing."""
        self.store.plays.delete_assignment(pending.job_handle, connection=connection)
        reason = self._invalid(pending, connection=connection)
        if reason is not None:
            logger.info("play binding of %s not applied: %s", pending.job_handle, reason)
            return False
        if self.membership(pending.participant_id, connection=connection) is None:
            self.store.plays.add_member(
                pending.participant_id,
                pending.instance_id,
                pending.position,
                created_at=now(),
                connection=connection,
            )
        return True

    def _invalid(self, pending: PlayAssignment, *, connection=None) -> str | None:
        """Why a binding no longer holds, or None: its play lives and its position still fits."""
        if self.store.plays.live_master(pending.instance_id, connection=connection) is None:
            return "the play it was sent in has ended"
        rules = self.store.plays.rules(pending.instance_id, connection=connection)
        if rules is not None:
            try:
                require_plugins(
                    rules,
                    store=self.store,
                    participant_id=pending.participant_id,
                    connection=connection,
                )
            except PlayRefused as exc:
                return str(exc)
        held = self.membership(pending.participant_id, connection=connection)
        if held is not None:
            if (held.instance_id, held.position) == (pending.instance_id, pending.position):
                return None
            return f"{pending.participant_id} now holds {held.rules.qualified(held.position)}"
        sender = self.membership(pending.assigned_by, connection=connection)
        target = self.store.get_participant(pending.participant_id, connection=connection)
        if (
            sender is None
            or sender.instance_id != pending.instance_id
            or pending.position not in sender.rules.children(sender.position)
            or target is None
            or target.parent_id != pending.assigned_by
        ):
            return f"{pending.assigned_by} can no longer give {pending.participant_id} a position"
        return None


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


def _seat_outside_a_play(parent_id: str | None, plan: PlayPlan) -> SpawnSeat:
    """Outside a live play only a master skill seats anyone: it starts the child's own play."""
    if plan.start is None:
        if plan.names:
            raise PlayRefused(_unseated(f"{parent_id or 'the operator'} is in no live play", plan))
        return NO_SEAT
    master = plan.start.qualified(plan.start.master)
    if extra := [name for name in plan.names if name != master]:
        raise PlayRefused(
            f"a new master holds only {master}; attach {', '.join(extra)} to the agents it spawns"
        )
    return SpawnSeat(start=plan.start)


def _resumed_seat(
    parent_id: str | None,
    governing: PlayMembership | None,
    kept: PlayMembership,
    plan: PlayPlan,
) -> SpawnSeat:
    """A resumed member keeps its position while its master lives, under a parent whose position
    may parent it, so its lineage stays inside the play."""
    rules = kept.rules
    if (
        governing is None
        or governing.instance_id != kept.instance_id
        or governing.participant_id != parent_id
        or kept.position not in rules.children(governing.position)
    ):
        parents = [position for position, kids in rules.positions.items() if kept.position in kids]
        raise PlayRefused(
            f"the resumed session holds {rules.qualified(kept.position)} in the live "
            f"{rules.play} play under master {kept.master_id}; only an agent holding "
            f"{_qualified(rules, parents)} in that play can resume it while the master lives"
        )
    positions, roles, foreign = _members(plan.names, rules)
    if plan.start is not None or foreign or any(p != kept.position for p in positions):
        raise PlayRefused(
            f"a resumed member keeps {rules.qualified(kept.position)} while master "
            f"{kept.master_id} lives; attach no other position, play or master skill"
        )
    _require_role_fit(rules, roles, kept.position)
    return SpawnSeat(instance_id=kept.instance_id, position=kept.position)


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


def _unseated(why: str, plan: PlayPlan) -> str:
    masters = " or ".join(plan.masters) or "its master skill"
    return (
        f"{why}, so no play can seat {', '.join(plan.names)}. Start one first: load {masters} "
        "yourself, or attach it to a spawn to start a new master; positions then come from "
        "the spawns and sends of its members"
    )


def _contains(below: PlayMembership) -> str:
    return (
        f"{below.participant_id} below you holds {below.rules.qualified(below.position)} in the "
        f"live {below.play} play under master {below.master_id}; a play cannot contain another "
        "live play, so start yours once that one ends"
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
