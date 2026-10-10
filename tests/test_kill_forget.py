"""kill(forget=true): retire unbound orphans without terminal I/O, refuse anything embodied."""

from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import insert

from theater.constants.refusals import NEXT_FORGET
from theater.daemon.schema import launch_reservations, public_operations
from theater.models import Status
from theater.protocol import RemoteError


def _orphan(daemon, name: str | None = None):
    """A spawned child with no binding, whose parent and owner are dead."""
    parent = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    child = daemon.registry.create_spawned(
        harness="vibe", cwd="/tmp", parent_id=parent.id, name=name
    )
    daemon.registry.mark_dead(parent.id)
    return child


def _uncertain_spawn(daemon, participant_id: str) -> None:
    with daemon.store.write_unit() as unit:
        unit.connection.execute(
            insert(public_operations).values(
                operation_id="op-spawn",
                kind="spawn",
                actor_client_id="c",
                target_ids=json.dumps([participant_id]),
                state="uncertain",
                phase="terminal_create_outcome_unknown",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        unit.connection.execute(
            insert(launch_reservations).values(
                operation_id="op-spawn",
                participant_id=participant_id,
                provider_id="provider-fixture",
                adapter="vibe",
                phase="terminal_create_dispatched",
                launch_facts="{}",
                artifact_refs="[]",
                created_at=1.0,
                updated_at=1.0,
            )
        )


async def test_any_live_participant_forgets_an_unbound_orphan(client, daemon):
    child = _orphan(daemon, "ghost")
    stranger = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    daemon.jobs.create(handle="s-1", caller_id=stranger.id, target_id=child.id, kind="send")

    # Without forget the presence gate can never pass, and next says so.
    with pytest.raises(RemoteError) as refused:
        await client.call("participant.kill", id=child.id)
    assert refused.value.code == "human_present"
    assert refused.value.details["next"] == NEXT_FORGET

    result = await client.call("participant.kill", id=child.id, caller_id=stranger.id, forget=True)
    assert result == {"id": child.id, "killed": True, "forgotten": True}
    row = daemon.store.get_participant(child.id)
    assert (row.status, row.termination_reason) == (Status.DEAD, "forgotten")
    assert daemon.registry.projection_name(child.id) is None
    job = daemon.store.get_job("s-1")
    assert (job.state, job.result) == ("killed", "target forgotten")


async def test_forget_refuses_a_bound_or_live_participant(client, daemon, terminal_provider):
    bound = _orphan(daemon)
    terminal_provider.bind(daemon, bound.id)
    live = _orphan(daemon)
    live.pid = os.getpid()
    daemon.store.upsert_participant(live)

    for target, needle in ((bound.id, "terminal binding"), (live.id, "is alive")):
        with pytest.raises(RemoteError) as refused:
            await client.call("participant.kill", id=target, forget=True)
        assert refused.value.code == "bad_request"
        assert needle in refused.value.message
        assert daemon.store.get_participant(target).status is not Status.DEAD


async def test_only_the_operator_forgets_an_uncertain_spawn(client, daemon):
    child = _orphan(daemon)
    _uncertain_spawn(daemon, child.id)
    stranger = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")

    with pytest.raises(RemoteError) as refused:
        await client.call("participant.kill", id=child.id, caller_id=stranger.id, forget=True)
    assert "only the local operator" in refused.value.message

    result = await client.call("participant.kill", id=child.id, forget=True)
    assert result["forgotten"] is True
