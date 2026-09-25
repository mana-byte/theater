"""Adoption: reparent a live sibling, reown it, and move the control gates with it."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

import pytest

from theater.config import RailsSection
from theater.daemon.control_ownership import ControlTransferService, OwnershipConflict
from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.frontend.participant_mutation_handlers import participants_terminate
from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.daemon.rails import BudgetExceeded, CycleDetected, DepthExceeded
from theater.daemon.rpc import METHODS
from theater.daemon.rpc.participants import _authorize_termination
from theater.daemon.runtime import control_gates
from theater.daemon.spawning.provider_launch import ParticipantLaunchService
from theater.frontend.capabilities import ConnectionChannel, ConnectionRole
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
)
from theater.models import (
    Busy,
    ControlOwnerKind,
    DeadTarget,
    JobState,
    NotAddressable,
    NotFound,
    NotSibling,
    NotYourChild,
    Participant,
    PublicOperationState,
    SelfAdopt,
    Status,
    now,
)


def _queue(
    daemon, participant_id: str, suffix: str, *, dispatched: bool = False
) -> tuple[str, str]:
    handle = f"{participant_id}#{suffix}"
    operation_id = f"public-{suffix}:control"
    daemon.jobs.create(
        handle=handle,
        caller_id="cli",
        target_id=participant_id,
        kind="send",
        prompt="queued",
        actor_client_id="original-client",
    )
    timestamp = now()
    daemon.store.reserve_control_operation(
        ControlOperation(
            operation_id=operation_id,
            participant_id=participant_id,
            kind=ControlKind.QUEUE_FOLLOWUP,
            transport=ControlTransport.PROVIDER_TERMINAL,
            delivery_phase=(
                ControlDeliveryPhase.DISPATCHED if dispatched else ControlDeliveryPhase.QUEUED
            ),
            job_handle=handle,
            provider_id="provider-a",
            provider_generation=4,
            terminal_id="terminal-a",
            terminal_incarnation="incarnation-a",
            queue_sequence=int(suffix),
            created_at=timestamp,
            updated_at=timestamp,
        )
    )
    return operation_id, handle


def _adopt(daemon, target: str, caller: str) -> Awaitable[dict]:
    return METHODS["participant.adopt"](daemon, {"id": target, "caller_id": caller})


def _spawn(daemon, **kwargs) -> Participant:
    return daemon.registry.create_spawned(harness="codex", cwd="/tmp", has_prompt=False, **kwargs)


def _context() -> ConnectionContext:
    return ConnectionContext(
        client_id="operator-a",
        role=ConnectionRole.OPERATOR,
        channel=ConnectionChannel.RPC,
        api_major=1,
        api_minor=0,
        capabilities=frozenset({"orchestration.v1"}),
    )


async def _settle(daemon) -> None:
    await asyncio.sleep(0)
    tasks = daemon.operation_service.owned_tasks
    if tasks:
        await asyncio.gather(*tasks)


async def test_adoption_reparents_a_sibling_reowns_it_and_moves_the_gates(
    daemon, terminal_provider
) -> None:
    parent = _spawn(daemon)
    adopter = _spawn(daemon, parent_id=parent.id)
    target = _spawn(daemon, parent_id=parent.id)
    grandchild = _spawn(daemon, parent_id=target.id)
    terminal_provider.bind(daemon, target.id)
    queued_id, queued_job = _queue(daemon, target.id, "1")
    dispatched_id, dispatched_job = _queue(daemon, target.id, "2", dispatched=True)

    record = await _adopt(daemon, target.id, adopter.id)

    adopted = daemon.registry.get(target.id)
    assert adopted.parent_id == adopter.id
    assert adopted.control_owner_kind is ControlOwnerKind.PARTICIPANT
    assert adopted.control_owner_id == adopter.id
    assert adopted.control_revision == 1
    assert record["id"] == target.id
    assert record["parent_id"] == adopter.id
    assert record["name"] == target.name  # the registry-decorated view, not the bare row
    assert daemon.registry.get(grandchild.id).parent_id == target.id  # subtree stays intact

    assert daemon.store.get_control_operation(queued_id).error_code == "control_transferred"
    assert daemon.store.get_job(queued_job).state == JobState.KILLED
    assert daemon.store.get_control_operation(dispatched_id).delivery_phase is (
        ControlDeliveryPhase.DISPATCHED
    )
    assert daemon.store.get_job(dispatched_job).state == JobState.RUNNING

    authorize = control_gates._authorize(daemon)
    authorize(target.id, adopter.id, "interrupt")
    with pytest.raises(NotYourChild, match="its parent is"):
        authorize(target.id, parent.id, "interrupt")

    _authorize_termination(daemon.registry.get(target.id), adopter.id)  # kill authority moved
    with pytest.raises(NotYourChild, match="current control owner"):
        _authorize_termination(daemon.registry.get(target.id), parent.id)

    owner_events = [
        event
        for group in daemon.store.journal.groups_after(0, limit=500)
        for event in group.events
        if event.kind == "participant.owner_changed" and event.entity_id == target.id
    ]
    assert owner_events[-1].payload["parent_id"] == adopter.id
    assert owner_events[-1].payload["owner"] == {
        "kind": "participant",
        "participant_id": adopter.id,
        "revision": 1,
    }


async def test_adoption_accepts_two_roots_as_siblings(daemon, terminal_provider) -> None:
    adopter = _spawn(daemon)
    target = _spawn(daemon)
    terminal_provider.bind(daemon, target.id)

    record = await _adopt(daemon, target.id, adopter.id)

    adopted = daemon.registry.get(target.id)
    assert adopted.parent_id == adopter.id
    assert adopted.control_owner_id == adopter.id
    assert record["parent_id"] == adopter.id


async def test_adoption_refusals_in_rule_order(daemon, terminal_provider) -> None:
    parent = _spawn(daemon)
    adopter = _spawn(daemon, parent_id=parent.id)
    target = _spawn(daemon, parent_id=parent.id)
    foreign = _spawn(daemon)  # a root: a different parent
    third_party = _spawn(daemon)

    with pytest.raises(SelfAdopt, match="that is you"):
        await _adopt(daemon, adopter.id, adopter.id)
    with pytest.raises(NotFound, match="no participant"):
        await _adopt(daemon, "missing-id", adopter.id)
    with pytest.raises(NotFound, match="no participant"):
        await _adopt(daemon, target.id, "missing-caller")

    daemon.registry.mark_dead(target.id)
    with pytest.raises(DeadTarget, match="it is dead"):
        await _adopt(daemon, target.id, adopter.id)
    revived = _spawn(daemon, parent_id=parent.id)
    with pytest.raises(NotAddressable, match="no verified terminal"):
        await _adopt(daemon, revived.id, adopter.id)
    terminal_provider.bind(daemon, revived.id)

    terminal_provider.bind(daemon, foreign.id)  # refusal order: sibling, not route
    with pytest.raises(NotSibling, match="its parent is"):
        await _adopt(daemon, foreign.id, adopter.id)

    with daemon.store.write_unit() as unit:
        ControlTransferService(daemon).transfer(
            [{"participant_id": revived.id, "expected_revision": 0}],
            {"kind": "participant", "participant_id": third_party.id},
            unit=unit,
        )
    with pytest.raises(OwnershipConflict, match="not steal control"):
        await _adopt(daemon, revived.id, adopter.id)

    for participant in (target, revived, third_party, foreign):
        daemon.registry.mark_dead(participant.id)
    named = _spawn(daemon, parent_id=parent.id)
    daemon.registry.rename(named.id, "Arlequin")
    terminal_provider.bind(daemon, named.id)
    record = await _adopt(daemon, "arlequin", adopter.id)  # a live name resolves
    assert record["parent_id"] == adopter.id

    dying_caller = _spawn(daemon, parent_id=parent.id)
    last_target = _spawn(daemon, parent_id=parent.id)
    terminal_provider.bind(daemon, last_target.id)
    daemon.registry.mark_dead(dying_caller.id)
    with pytest.raises(OwnershipConflict, match="is not live"):
        await _adopt(daemon, last_target.id, dying_caller.id)


async def test_adoption_refuses_depth_and_budget_overruns(
    daemon, terminal_provider, monkeypatch
) -> None:
    from dataclasses import replace

    root = _spawn(daemon)
    middle = _spawn(daemon, parent_id=root.id)
    deep_adopter = _spawn(daemon, parent_id=middle.id)
    sibling = _spawn(daemon, parent_id=middle.id)
    _spawn(daemon, parent_id=sibling.id)
    terminal_provider.bind(daemon, sibling.id)
    with pytest.raises(DepthExceeded, match="depth 4"):
        await _adopt(daemon, sibling.id, deep_adopter.id)

    monkeypatch.setattr(
        daemon, "config", replace(daemon.config, rails=RailsSection(depth_cap=3, budget=1))
    )
    shallow_adopter = _spawn(daemon)
    shallow_target = _spawn(daemon)
    terminal_provider.bind(daemon, shallow_target.id)
    with pytest.raises(BudgetExceeded, match="2 live participants"):
        await _adopt(daemon, shallow_target.id, shallow_adopter.id)

    # Siblings inside one tree merge nothing, so the budget never applies.
    parent = _spawn(daemon)
    first = _spawn(daemon, parent_id=parent.id)
    second = _spawn(daemon, parent_id=parent.id)
    terminal_provider.bind(daemon, second.id)
    record = await _adopt(daemon, second.id, first.id)
    assert record["parent_id"] == first.id


async def test_adoption_refuses_a_target_that_is_an_ancestor(
    daemon, terminal_provider, monkeypatch
) -> None:
    from dataclasses import replace

    monkeypatch.setattr(
        daemon, "config", replace(daemon.config, rails=RailsSection(depth_cap=10, budget=20))
    )
    parent = _spawn(daemon)
    adopter = _spawn(daemon, parent_id=parent.id)
    target = _spawn(daemon, parent_id=parent.id)
    # Corrupt the lineage: the shared parent becomes the target's child, so the
    # target is both a sibling and an ancestor of the adopter.
    daemon.store.reparent_participant(parent.id, new_parent_id=target.id)
    terminal_provider.bind(daemon, target.id)

    with pytest.raises(CycleDetected, match="ancestor"):
        await _adopt(daemon, target.id, adopter.id)


async def test_adoption_rolls_back_reparent_reown_queue_and_journal_together(
    daemon, terminal_provider, monkeypatch
) -> None:
    parent = _spawn(daemon)
    adopter = _spawn(daemon, parent_id=parent.id)
    target = _spawn(daemon, parent_id=parent.id)
    terminal_provider.bind(daemon, target.id)
    operation_id, handle = _queue(daemon, target.id, "1")
    before = daemon.store.journal.current_sequence()

    def fail_append(*_args, **_kwargs):
        raise RuntimeError("crash before commit")

    monkeypatch.setattr(daemon.store.journal, "append_group", fail_append)
    with pytest.raises(RuntimeError, match="crash before commit"):
        await _adopt(daemon, target.id, adopter.id)

    unchanged = daemon.registry.get(target.id)
    assert unchanged.parent_id == parent.id
    assert unchanged.control_owner_id == parent.id  # spawned children are owned by their parent
    assert unchanged.control_revision == 0
    assert daemon.store.get_control_operation(operation_id).delivery_phase is (
        ControlDeliveryPhase.QUEUED
    )
    assert daemon.store.get_job(handle).state == JobState.RUNNING
    assert daemon.store.journal.current_sequence() == before


async def test_adoption_refuses_a_control_ownership_cycle(daemon, terminal_provider) -> None:
    adopter = _spawn(daemon)
    target = _spawn(daemon)
    terminal_provider.bind(daemon, target.id)
    with daemon.store.write_unit() as unit:
        ControlTransferService(daemon).transfer(
            [{"participant_id": adopter.id, "expected_revision": 0}],
            {"kind": "participant", "participant_id": target.id},
            unit=unit,
        )

    with pytest.raises(OwnershipConflict, match="adoption would create an ownership cycle"):
        await _adopt(daemon, target.id, adopter.id)


async def test_adoption_refuses_in_the_termination_acceptance_gap(
    daemon, terminal_provider
) -> None:
    parent = _spawn(daemon)
    adopter = _spawn(daemon, parent_id=parent.id)
    target = _spawn(daemon, parent_id=parent.id)
    terminal_provider.bind(daemon, target.id)

    accepted = await participants_terminate(
        daemon, _context(), {"participant_id": target.id}, idempotency_key="adoption-gap-a"
    )
    assert isinstance(accepted, dict)
    assert accepted["state"] == "accepted"  # the side effect has not started yet
    with pytest.raises(Busy, match="still unsettled"):
        await _adopt(daemon, target.id, adopter.id)

    await _settle(daemon)  # let the refused kill finish
    operation = daemon.operation_service.get(accepted["operation_id"])
    assert operation.state == PublicOperationState.SUCCEEDED.value
    killed = daemon.registry.get(target.id)
    assert killed.status is Status.DEAD
    assert killed.parent_id == parent.id  # died unreowned: the refusal held


async def test_adoption_refuses_while_a_termination_is_uncertain(
    daemon, terminal_provider, monkeypatch
) -> None:
    parent = _spawn(daemon)
    adopter = _spawn(daemon, parent_id=parent.id)
    spare = _spawn(daemon, parent_id=parent.id)
    for participant_id in (adopter.id, spare.id):
        terminal_provider.bind(daemon, participant_id)

    async def unverified(participant_id, *, caller_id, callback_operation_id):
        return {"delivery": "unknown"}  # the provider cannot confirm the exit

    monkeypatch.setattr(daemon.controls, "terminate_provider", unverified)
    accepted = await participants_terminate(
        daemon, _context(), {"participant_id": adopter.id}, idempotency_key="adoption-uncertain-a"
    )
    await _settle(daemon)
    assert isinstance(accepted, dict)
    operation = daemon.operation_service.get(accepted["operation_id"])
    assert operation.state == PublicOperationState.UNCERTAIN.value
    assert daemon.registry.get(adopter.id).status is not Status.DEAD  # outcome unknown

    with pytest.raises(Busy, match="still unsettled"):
        await _adopt(daemon, spare.id, adopter.id)


async def test_spawn_rails_recheck_refuses_a_child_of_a_mid_flight_adoption(
    daemon, terminal_provider, monkeypatch, tmp_path
) -> None:
    root = _spawn(daemon)
    middle = _spawn(daemon, parent_id=root.id)
    adopter = _spawn(daemon, parent_id=middle.id)
    target = _spawn(daemon, parent_id=middle.id)
    terminal_provider.bind(daemon, target.id)
    real_prepare = ParticipantLaunchService._prepare_workspace_for_spawn

    async def prepare_then_adopt(self, params):
        preparation = await real_prepare(self, params)
        await _adopt(daemon, target.id, adopter.id)
        return preparation

    monkeypatch.setattr(
        ParticipantLaunchService, "_prepare_workspace_for_spawn", prepare_then_adopt
    )
    # depth_of(target) is 2, so the early check admits a depth-3 child; the mid-flight
    # adoption moves target to depth 3 and the reservation recheck must refuse.
    with pytest.raises(DepthExceeded, match="depth 4"):
        await METHODS["spawn"](
            daemon,
            {
                "harness": "codex",
                "approval": "manual",
                "cwd": str(tmp_path),
                "parent_id": target.id,
            },
        )
    assert len(daemon.registry.list()) == 4  # no child was reserved
