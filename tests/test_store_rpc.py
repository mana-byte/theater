"""Daemon RPC tests for the tree-scoped scratchpad."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from theater.daemon.schema import tree_kv
from theater.protocol import RemoteError


def _repo(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


async def test_root_caller_can_write_and_get(client, tmp_path):
    repo = _repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    wrote = await client.call(
        "scratchpad.write",
        caller_id=caller["id"],
        namespace="plan",
        value="ship it",
    )
    assert wrote["namespace"] == "plan"
    assert isinstance(wrote["key"], str) and len(wrote["key"]) > 0

    got = await client.call(
        "scratchpad.get",
        caller_id=caller["id"],
        namespace="plan",
    )
    assert got == {"namespace": "plan", "entries": {wrote["key"]: "ship it"}}


async def test_descendants_and_siblings_share_scratchpad(client, daemon, tmp_path):
    repo = _repo(tmp_path, "repo")
    root = daemon.registry.create_spawned(harness="vibe", cwd=str(repo), pid="root")
    child = daemon.registry.create_spawned(
        harness="vibe", cwd=str(repo), parent_id=root.id, pid="child"
    )
    sibling = daemon.registry.create_spawned(
        harness="vibe", cwd=str(repo), parent_id=root.id, pid="sibling"
    )

    wrote = await client.call(
        "scratchpad.write",
        caller_id=child.id,
        namespace="handoff",
        value="ready",
    )

    got = await client.call(
        "scratchpad.get",
        caller_id=sibling.id,
        namespace="handoff",
    )
    assert got == {"namespace": "handoff", "entries": {wrote["key"]: "ready"}}


async def test_scratchpad_is_isolated_between_trees(client, daemon, tmp_path):
    repo = _repo(tmp_path, "repo")
    first = daemon.registry.create_spawned(harness="vibe", cwd=str(repo), pid="first")
    second = daemon.registry.create_spawned(harness="vibe", cwd=str(repo), pid="second")

    await client.call(
        "scratchpad.write",
        caller_id=first.id,
        namespace="handoff",
        value="first tree",
    )

    got = await client.call(
        "scratchpad.get",
        caller_id=second.id,
        namespace="handoff",
    )
    assert got == {"namespace": "handoff", "entries": {}}


async def test_scratchpad_is_isolated_between_repo_roots(client, daemon, tmp_path):
    repo_a = _repo(tmp_path, "repo-a")
    repo_b = _repo(tmp_path, "repo-b")
    root = daemon.registry.create_spawned(harness="vibe", cwd=str(repo_a), pid="root")
    child = daemon.registry.create_spawned(
        harness="vibe", cwd=str(repo_b), parent_id=root.id, pid="child"
    )

    await client.call(
        "scratchpad.write",
        caller_id=root.id,
        namespace="handoff",
        value="repo a",
    )

    got = await client.call(
        "scratchpad.get",
        caller_id=child.id,
        namespace="handoff",
    )
    assert got == {"namespace": "handoff", "entries": {}}


async def test_scratchpad_write_with_key_updates_existing(client, tmp_path):
    repo = _repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    first = await client.call(
        "scratchpad.write",
        caller_id=caller["id"],
        namespace="notes",
        value="first",
    )
    second = await client.call(
        "scratchpad.write",
        caller_id=caller["id"],
        namespace="notes",
        value="second",
        key=first["key"],
    )
    assert second == {"namespace": "notes", "key": first["key"]}

    got = await client.call(
        "scratchpad.get",
        caller_id=caller["id"],
        namespace="notes",
    )
    assert got == {"namespace": "notes", "entries": {first["key"]: "second"}}


async def test_scratchpad_write_with_nonexistent_key_inserts(client, tmp_path):
    repo = _repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    wrote = await client.call(
        "scratchpad.write",
        caller_id=caller["id"],
        namespace="notes",
        value="inserted",
        key="custom-key",
    )
    assert wrote == {"namespace": "notes", "key": "custom-key"}

    got = await client.call(
        "scratchpad.get",
        caller_id=caller["id"],
        namespace="notes",
    )
    assert got == {"namespace": "notes", "entries": {"custom-key": "inserted"}}


async def test_scratchpad_get_rejects_non_string_keys_elements(client, tmp_path):
    repo = _repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "scratchpad.get",
            caller_id=caller["id"],
            namespace="notes",
            keys=[123],
        )
    assert exc.value.code == "bad_request"
    assert "list of strings" in str(exc.value)


async def test_scratchpad_get_with_keys_filters(client, tmp_path):
    repo = _repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    a = await client.call(
        "scratchpad.write",
        caller_id=caller["id"],
        namespace="notes",
        value="a",
    )
    b = await client.call(
        "scratchpad.write",
        caller_id=caller["id"],
        namespace="notes",
        value="b",
    )

    got = await client.call(
        "scratchpad.get",
        caller_id=caller["id"],
        namespace="notes",
        keys=[a["key"]],
    )
    assert got == {"namespace": "notes", "entries": {a["key"]: "a"}}

    all_got = await client.call(
        "scratchpad.get",
        caller_id=caller["id"],
        namespace="notes",
    )
    assert set(all_got["entries"].keys()) == {a["key"], b["key"]}


async def test_scratchpad_get_missing_keys_returns_empty(client, tmp_path):
    repo = _repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    got = await client.call(
        "scratchpad.get",
        caller_id=caller["id"],
        namespace="notes",
        keys=["nonexistent"],
    )
    assert got == {"namespace": "notes", "entries": {}}


async def test_scratchpad_refuses_callers_outside_git(client, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(outside))

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "scratchpad.write",
            caller_id=caller["id"],
            namespace="notes",
            value="nope",
        )

    assert exc.value.code == "bad_request"
    assert "outside a git repository" in str(exc.value)


async def test_scratchpad_requires_existing_caller(client, tmp_path):
    repo = _repo(tmp_path, "repo")
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "scratchpad.write",
            caller_id="ghost",
            namespace="notes",
            value=str(repo),
        )

    assert exc.value.code == "bad_request"
    assert "existing participant" in str(exc.value)


async def test_scratchpad_write_ignores_client_supplied_updated_by(client, daemon, tmp_path):
    repo = _repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    await client.call(
        "scratchpad.write",
        caller_id=caller["id"],
        namespace="notes",
        value="mine",
        updated_by="somebody-else",
    )

    row = daemon.store.conn.execute(tree_kv.select()).first()
    assert row is not None
    assert row._mapping["updated_by"] == caller["id"]


# ---- participants.recent_dead ------------------------------------------------


async def test_recent_dead_returns_dead_participants_with_spawn_prompt(client, daemon, tmp_path):
    repo = _repo(tmp_path, "repo")
    await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    child = await client.call(
        "spawn",
        harness="vibe",
        approval="manual",
        prompt="review the code",
        cwd=str(repo),
        tmux_session="test",
    )
    child_id = child["id"]

    daemon.registry.mark_dead(child_id)

    rows = await client.call("participants.recent_dead", limit=20)
    matching = [r for r in rows if r["id"] == child_id]
    assert len(matching) == 1
    row = matching[0]
    assert row["status"] == "dead"
    assert row["spawn_prompt"] == "review the code"
    assert "resume_state" in row


async def test_recent_dead_empty_when_no_dead(client, tmp_path):
    repo = _repo(tmp_path, "repo")
    await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    rows = await client.call("participants.recent_dead")
    assert rows == []


async def test_recent_dead_rejects_invalid_limit(client, tmp_path):
    repo = _repo(tmp_path, "repo")
    await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    with pytest.raises(RemoteError) as exc:
        await client.call("participants.recent_dead", limit=0)
    assert exc.value.code == "bad_request"

    with pytest.raises(RemoteError) as exc:
        await client.call("participants.recent_dead", limit=21)
    assert exc.value.code == "bad_request"


async def test_recent_dead_spawn_prompt_null_for_bare_cli(client, daemon, tmp_path):
    repo = _repo(tmp_path, "repo")
    await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    child = await client.call(
        "spawn",
        harness="vibe",
        approval="manual",
        prompt="",
        cwd=str(repo),
        tmux_session="test",
    )
    child_id = child["id"]

    daemon.registry.mark_dead(child_id)

    rows = await client.call("participants.recent_dead", limit=20)
    matching = [r for r in rows if r["id"] == child_id]
    assert len(matching) == 1
    assert matching[0]["spawn_prompt"] == ""
