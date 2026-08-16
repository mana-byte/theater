"""Daemon RPC tests for the tree-scoped key/value store."""

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


async def test_root_caller_can_put_and_get_value(client, tmp_path):
    repo = _repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    assert await client.call(
        "store.put",
        caller_id=caller["id"],
        namespace="plan",
        key="next",
        value="ship it",
    ) == {"stored": True}
    assert await client.call(
        "store.get",
        caller_id=caller["id"],
        namespace="plan",
        key="next",
    ) == {"value": "ship it"}


async def test_descendants_and_siblings_share_tree_value(client, daemon, tmp_path):
    repo = _repo(tmp_path, "repo")
    root = daemon.registry.create_spawned(harness="vibe", cwd=str(repo), pid="root")
    child = daemon.registry.create_spawned(
        harness="vibe", cwd=str(repo), parent_id=root.id, pid="child"
    )
    sibling = daemon.registry.create_spawned(
        harness="vibe", cwd=str(repo), parent_id=root.id, pid="sibling"
    )

    await client.call(
        "store.put",
        caller_id=child.id,
        namespace="handoff",
        key="summary",
        value="ready",
    )

    assert await client.call(
        "store.get",
        caller_id=sibling.id,
        namespace="handoff",
        key="summary",
    ) == {"value": "ready"}


async def test_tree_store_is_isolated_between_trees(client, daemon, tmp_path):
    repo = _repo(tmp_path, "repo")
    first = daemon.registry.create_spawned(harness="vibe", cwd=str(repo), pid="first")
    second = daemon.registry.create_spawned(harness="vibe", cwd=str(repo), pid="second")

    await client.call(
        "store.put",
        caller_id=first.id,
        namespace="handoff",
        key="summary",
        value="first tree",
    )

    assert await client.call(
        "store.get",
        caller_id=second.id,
        namespace="handoff",
        key="summary",
    ) == {"value": None}


async def test_tree_store_is_isolated_between_repo_roots(client, daemon, tmp_path):
    repo_a = _repo(tmp_path, "repo-a")
    repo_b = _repo(tmp_path, "repo-b")
    root = daemon.registry.create_spawned(harness="vibe", cwd=str(repo_a), pid="root")
    child = daemon.registry.create_spawned(
        harness="vibe", cwd=str(repo_b), parent_id=root.id, pid="child"
    )

    await client.call(
        "store.put",
        caller_id=root.id,
        namespace="handoff",
        key="summary",
        value="repo a",
    )

    assert await client.call(
        "store.get",
        caller_id=child.id,
        namespace="handoff",
        key="summary",
    ) == {"value": None}


async def test_tree_store_upsert_replaces_value(client, tmp_path):
    repo = _repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))
    for value in ("first", "second"):
        await client.call(
            "store.put",
            caller_id=caller["id"],
            namespace="notes",
            key="latest",
            value=value,
        )

    assert await client.call(
        "store.get",
        caller_id=caller["id"],
        namespace="notes",
        key="latest",
    ) == {"value": "second"}


async def test_tree_store_missing_value_returns_none(client, tmp_path):
    repo = _repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    assert await client.call(
        "store.get",
        caller_id=caller["id"],
        namespace="notes",
        key="missing",
    ) == {"value": None}


async def test_tree_store_refuses_callers_outside_git(client, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(outside))

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "store.put",
            caller_id=caller["id"],
            namespace="notes",
            key="latest",
            value="nope",
        )

    assert exc.value.code == "bad_request"
    assert "outside a git repository" in str(exc.value)


async def test_tree_store_requires_existing_caller(client, tmp_path):
    repo = _repo(tmp_path, "repo")
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "store.put",
            caller_id="ghost",
            namespace="notes",
            key="latest",
            value=str(repo),
        )

    assert exc.value.code == "bad_request"
    assert "existing participant" in str(exc.value)


async def test_tree_store_ignores_client_supplied_updated_by(client, daemon, tmp_path):
    repo = _repo(tmp_path, "repo")
    caller = await client.call("hello", id="root", harness="vibe", cwd=str(repo))

    await client.call(
        "store.put",
        caller_id=caller["id"],
        namespace="notes",
        key="latest",
        value="mine",
        updated_by="somebody-else",
    )

    row = daemon.store.conn.execute(tree_kv.select()).first()
    assert row is not None
    assert row._mapping["updated_by"] == caller["id"]
