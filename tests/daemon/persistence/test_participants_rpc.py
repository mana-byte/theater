"""Participant RPCs: scoped updates and recently dead sessions."""

from __future__ import annotations

import pytest

from tests.support.git_repos import init_repo
from theater.protocol import RemoteError


async def test_participant_update_is_scoped_atomic_and_bounded(client, daemon):
    parent = await client.call("hello", id="parent", harness="vibe", cwd="/tmp")
    child = daemon.registry.create_spawned(
        harness="vibe",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    sibling = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    original_name = child.name

    with pytest.raises(RemoteError) as exc:
        await client.call("participant.update", caller_id=parent["id"])
    assert exc.value.code == "bad_request"

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "participant.update",
            caller_id=parent["id"],
            target=child.id,
            name="Metadata-Child",
            description="bad\nsummary",
        )
    assert exc.value.code == "bad_request"
    unchanged = daemon.registry.get(child.id)
    assert (unchanged.name, unchanged.description) == (original_name, None)

    updated = await client.call(
        "participant.update",
        caller_id=parent["id"],
        target=child.name,
        name="Metadata-Child",
        description="  Implement metadata  ",
    )
    assert (updated["name"], updated["description"]) == ("Metadata-Child", "Implement metadata")
    events = [
        row
        for row in await client.call("bus.tail", limit=50)
        if row["kind"] == "participant.metadata_changed" and row["to_id"] == child.id
    ]
    assert len(events) == 1
    assert events[0]["payload"] == {"fields": ["description", "name"]}

    cleared = await client.call(
        "participant.update",
        caller_id=parent["id"],
        description="",
    )
    assert cleared["id"] == parent["id"]
    assert cleared["description"] is None

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "participant.update",
            caller_id=parent["id"],
            target=sibling.id,
            description="not yours",
        )
    assert exc.value.code == "not_your_child"

    daemon.registry.mark_dead(child.id)
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "participant.update",
            caller_id=parent["id"],
            target=child.id,
            description="too late",
        )
    assert exc.value.code == "bad_request"


async def test_recent_dead_returns_dead_participants_with_spawn_prompt(client, daemon, tmp_path):
    repo = init_repo(tmp_path, "repo")
    await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    child = daemon.registry.create_spawned(harness="vibe", cwd=str(repo))
    daemon.jobs.create(
        handle=child.id,
        caller_id="cli",
        target_id=child.id,
        kind="spawn",
        prompt="review the code",
    )
    child_id = child.id

    p = daemon.registry.get(child_id)
    p.session_id = "test-session-123"
    daemon.store.upsert_participant(p)

    daemon.registry.mark_dead(child_id)

    rows = await client.call("participants.recent_dead", limit=20)
    matching = [r for r in rows if r["id"] == child_id]
    assert len(matching) == 1
    row = matching[0]
    assert row["status"] == "dead"
    assert row["spawn_prompt"] == "review the code"
    assert "resume_state" in row


async def test_recent_dead_empty_when_no_dead(client, tmp_path):
    repo = init_repo(tmp_path, "repo")
    await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    rows = await client.call("participants.recent_dead")
    assert rows == []


async def test_recent_dead_rejects_invalid_limit(client, tmp_path):
    repo = init_repo(tmp_path, "repo")
    await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    with pytest.raises(RemoteError) as exc:
        await client.call("participants.recent_dead", limit=0)
    assert exc.value.code == "bad_request"

    with pytest.raises(RemoteError) as exc:
        await client.call("participants.recent_dead", limit=21)
    assert exc.value.code == "bad_request"


async def test_recent_dead_spawn_prompt_null_for_bare_cli(client, daemon, tmp_path):
    repo = init_repo(tmp_path, "repo")
    await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    child = daemon.registry.create_spawned(harness="vibe", cwd=str(repo))
    daemon.jobs.create(
        handle=child.id,
        caller_id="cli",
        target_id=child.id,
        kind="spawn",
        prompt="",
    )
    child_id = child.id

    p = daemon.registry.get(child_id)
    p.session_id = "bare-session-456"
    daemon.store.upsert_participant(p)

    daemon.registry.mark_dead(child_id)

    rows = await client.call("participants.recent_dead", limit=20)
    matching = [r for r in rows if r["id"] == child_id]
    assert len(matching) == 1
    assert matching[0]["spawn_prompt"] == ""


async def test_recent_dead_excludes_sessions_without_session_id(client, daemon, tmp_path):
    repo = init_repo(tmp_path, "repo")
    await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    child = daemon.registry.create_spawned(harness="vibe", cwd=str(repo))
    daemon.jobs.create(
        handle=child.id,
        caller_id="cli",
        target_id=child.id,
        kind="spawn",
        prompt="no session id here",
    )
    child_id = child.id

    daemon.registry.mark_dead(child_id)

    rows = await client.call("participants.recent_dead", limit=20)
    matching = [r for r in rows if r["id"] == child_id]
    assert matching == []
