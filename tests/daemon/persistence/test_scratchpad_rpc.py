"""Scratchpad RPCs: writes, reads and sharing across callers and trees."""

from __future__ import annotations

import pytest

from tests.support.git_repos import init_repo
from theater.daemon.schema import global_scratchpad
from theater.protocol import RemoteError


async def test_root_caller_can_write_and_get(client, tmp_path):
    repo = init_repo(tmp_path, "repo")
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
    assert got == {
        "namespace": "plan",
        "entries": {wrote["key"]: "ship it"},
        "keys": [wrote["key"]],
        "truncated": False,
        "after_key": None,
    }


async def test_descendants_and_siblings_share_scratchpad(client, daemon, tmp_path):
    repo = init_repo(tmp_path, "repo")
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
    assert got == {
        "namespace": "handoff",
        "entries": {wrote["key"]: "ready"},
        "keys": [wrote["key"]],
        "truncated": False,
        "after_key": None,
    }


async def test_scratchpad_is_shared_between_trees(client, daemon, tmp_path):
    repo = init_repo(tmp_path, "repo")
    first = daemon.registry.create_spawned(harness="vibe", cwd=str(repo), pid="first")
    second = daemon.registry.create_spawned(harness="vibe", cwd=str(repo), pid="second")

    wrote = await client.call(
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
    assert got == {
        "namespace": "handoff",
        "entries": {wrote["key"]: "first tree"},
        "keys": [wrote["key"]],
        "truncated": False,
        "after_key": None,
    }


async def test_scratchpad_is_shared_between_repo_roots(client, daemon, tmp_path):
    repo_a = init_repo(tmp_path, "repo-a")
    repo_b = init_repo(tmp_path, "repo-b")
    root = daemon.registry.create_spawned(harness="vibe", cwd=str(repo_a), pid="root")
    child = daemon.registry.create_spawned(
        harness="vibe", cwd=str(repo_b), parent_id=root.id, pid="child"
    )

    wrote = await client.call(
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
    assert got == {
        "namespace": "handoff",
        "entries": {wrote["key"]: "repo a"},
        "keys": [wrote["key"]],
        "truncated": False,
        "after_key": None,
    }


async def test_scratchpad_write_with_key_updates_existing(client, tmp_path):
    repo = init_repo(tmp_path, "repo")
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
    assert got == {
        "namespace": "notes",
        "entries": {first["key"]: "second"},
        "keys": [first["key"]],
        "truncated": False,
        "after_key": None,
    }


async def test_scratchpad_write_with_nonexistent_key_inserts(client, tmp_path):
    repo = init_repo(tmp_path, "repo")
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
    assert got == {
        "namespace": "notes",
        "entries": {"custom-key": "inserted"},
        "keys": ["custom-key"],
        "truncated": False,
        "after_key": None,
    }


async def test_scratchpad_get_rejects_non_string_keys_elements(client, tmp_path):
    repo = init_repo(tmp_path, "repo")
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
    repo = init_repo(tmp_path, "repo")
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
    assert got == {
        "namespace": "notes",
        "entries": {a["key"]: "a"},
        "keys": [a["key"]],
        "truncated": False,
        "after_key": None,
    }

    all_got = await client.call(
        "scratchpad.get",
        caller_id=caller["id"],
        namespace="notes",
    )
    assert set(all_got["entries"].keys()) == {a["key"], b["key"]}


async def test_scratchpad_get_missing_keys_returns_empty(client, tmp_path):
    repo = init_repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    got = await client.call(
        "scratchpad.get",
        caller_id=caller["id"],
        namespace="notes",
        keys=["nonexistent"],
    )
    assert got == {
        "namespace": "notes",
        "entries": {},
        "keys": [],
        "truncated": False,
        "after_key": None,
    }


async def test_scratchpad_accepts_callers_outside_git(client, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(outside))

    wrote = await client.call(
        "scratchpad.write",
        caller_id=caller["id"],
        namespace="notes",
        value="works outside git",
    )
    assert (await client.call("scratchpad.get", namespace="notes"))["entries"] == {
        wrote["key"]: "works outside git"
    }


async def test_scratchpad_does_not_require_an_existing_caller(client, tmp_path):
    repo = init_repo(tmp_path, "repo")
    wrote = await client.call(
        "scratchpad.write",
        caller_id="ghost",
        namespace="notes",
        value=str(repo),
    )
    assert (await client.call("scratchpad.get", namespace="notes"))["entries"] == {
        wrote["key"]: str(repo)
    }


async def test_scratchpad_write_ignores_client_supplied_updated_by(client, daemon, tmp_path):
    repo = init_repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    await client.call(
        "scratchpad.write",
        caller_id=caller["id"],
        namespace="notes",
        value="mine",
        updated_by="somebody-else",
    )

    row = daemon.store.conn.execute(global_scratchpad.select()).first()
    assert row is not None
    assert row._mapping["actor_participant_id"] == caller["id"]
