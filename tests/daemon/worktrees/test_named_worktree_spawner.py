"""Spawner create-or-join of named worktrees: stale rows, retention, launch failure and locking."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from tests.support.git_repos import committed_repo
from theater.daemon import worktree as wt
from theater.daemon.spawning.service import Spawner
from theater.models import BadRequest


@pytest.fixture
def repo(tmp_path):
    """A real git repo with one commit."""
    return committed_repo(tmp_path)


def _named_participant(child_id: str, cwd: str, name: str):
    from theater.models import Participant, Tier

    return Participant(
        id=child_id,
        harness="vibe",
        tier=Tier.SPAWNED,
        cwd=cwd,
        branch=wt.named_branch_name(name),
    )


async def test_named_worktree_spawner_creates_and_joins(repo, store):
    """Two spawns with the same name share the same directory and branch."""
    from theater.daemon.store import Store

    # We need a Store to test the persistence layer. Use the repo's
    # .theater dir for the db.
    db_path = Path(repo) / ".theater" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    s = Store(db_path)
    try:
        registry = type("R", (), {"store": s})()
        spawner = Spawner(registry=registry)

        path1, branch1 = await spawner._spawn_named_worktree(
            root=repo, name="shared", base_branch=None
        )
        # The named_worktrees row should exist
        row = s.get_named_worktree(repo_root=repo, name="shared")
        assert row is not None
        assert row["path"] == path1
        assert row["branch"] == branch1

        # Second spawn with same name joins
        path2, branch2 = await spawner._spawn_named_worktree(
            root=repo, name="shared", base_branch=None
        )
        assert path2 == path1
        assert branch2 == branch1

        assert Path(path1).exists()
    finally:
        s.close()


async def test_named_worktree_spawner_refuses_conflicting_base_branch(repo, store):
    """A later join with a conflicting base_branch is refused."""
    from theater.daemon.store import Store

    db_path = Path(repo) / ".theater" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    s = Store(db_path)
    try:
        registry = type("R", (), {"store": s})()
        spawner = Spawner(registry=registry)

        await spawner._spawn_named_worktree(root=repo, name="conflict", base_branch="main")
        with pytest.raises(BadRequest, match="base_branch"):
            await spawner._spawn_named_worktree(root=repo, name="conflict", base_branch="feature")
    finally:
        s.close()


async def test_named_worktree_persists_across_restart(repo, store):
    """A daemon restart recognises a named worktree from the table."""
    from theater.daemon.store import Store

    db_path = Path(repo) / ".theater" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    s = Store(db_path)
    try:
        registry = type("R", (), {"store": s})()
        spawner = Spawner(registry=registry)

        path, branch = await spawner._spawn_named_worktree(
            root=repo, name="survivor", base_branch=None
        )
    finally:
        s.close()

    # Simulate a restart: new Store on the same db
    s2 = Store(db_path)
    try:
        registry2 = type("R", (), {"store": s2})()
        spawner2 = Spawner(registry=registry2)

        # Join should find the existing named worktree
        path2, branch2 = await spawner2._spawn_named_worktree(
            root=repo, name="survivor", base_branch=None
        )
        assert path2 == path
        assert branch2 == branch
    finally:
        s2.close()


async def test_named_worktree_canonical_scope_from_linked_worktree(repo, store):
    """A named worktree created from inside another linked worktree must
    key and locate itself under the canonical main repository."""
    from theater.daemon.store import Store

    # First create a regular True worktree to simulate being inside a linked worktree
    inner_wt = wt.create_worktree(repo_root=repo, child_id="innerwt")

    db_path = Path(repo) / ".theater" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    s = Store(db_path)
    try:
        registry = type("R", (), {"store": s})()
        spawner = Spawner(registry=registry)

        # Spawn a named worktree from inside the linked worktree
        path, branch = await spawner._spawn_named_worktree(
            root=inner_wt, name="from-linked", base_branch=None
        )

        # The path should be under the canonical main repo, not under the linked worktree
        assert ".theater/worktrees/named/from-linked" in path
        assert path.startswith(repo)
        assert not path.startswith(inner_wt)

        # The named_worktrees row should be keyed by the canonical root
        row = s.get_named_worktree(repo_root=repo, name="from-linked")
        assert row is not None
        assert row["path"] == path

        # A second join from inside the linked worktree should find the same one
        path2, branch2 = await spawner._spawn_named_worktree(
            root=inner_wt, name="from-linked", base_branch=None
        )
        assert path2 == path
        assert branch2 == branch
    finally:
        s.close()


async def test_named_worktree_base_branch_none_rejects_explicit_on_join(repo, store):
    """If persisted base_branch is None, an explicit base_branch on join is rejected."""
    from theater.daemon.store import Store

    db_path = Path(repo) / ".theater" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    s = Store(db_path)
    try:
        registry = type("R", (), {"store": s})()
        spawner = Spawner(registry=registry)

        # Create with base_branch=None
        await spawner._spawn_named_worktree(root=repo, name="bb-none", base_branch=None)

        # Join with an explicit base_branch should be rejected
        with pytest.raises(BadRequest, match="base_branch"):
            await spawner._spawn_named_worktree(root=repo, name="bb-none", base_branch="main")
    finally:
        s.close()


async def test_named_worktree_base_branch_exact_match_allows_join(repo, store):
    """An explicit base_branch that exactly equals the persisted value allows join."""
    from theater.daemon.store import Store

    db_path = Path(repo) / ".theater" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    s = Store(db_path)
    try:
        registry = type("R", (), {"store": s})()
        spawner = Spawner(registry=registry)

        # Create with base_branch="main"
        await spawner._spawn_named_worktree(root=repo, name="bb-main", base_branch="main")

        # Join with the same base_branch should succeed
        path2, branch2 = await spawner._spawn_named_worktree(
            root=repo, name="bb-main", base_branch="main"
        )
        assert path2 is not None
        assert branch2 is not None
    finally:
        s.close()


async def test_named_worktree_join_refused_missing_path(repo, store):
    """Joining a named worktree whose directory was deleted is refused."""
    from theater.daemon.store import Store

    db_path = Path(repo) / ".theater" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    s = Store(db_path)
    try:
        registry = type("R", (), {"store": s})()
        spawner = Spawner(registry=registry)

        path, _branch = await spawner._spawn_named_worktree(
            root=repo, name="stale-path", base_branch=None
        )

        # Delete the directory out from under us
        import shutil

        shutil.rmtree(path)

        with pytest.raises(BadRequest, match="does not exist"):
            await spawner._spawn_named_worktree(root=repo, name="stale-path", base_branch=None)
    finally:
        s.close()


async def test_named_worktree_join_refused_wrong_branch(repo, store):
    """Joining a named worktree where the checked-out branch was switched is refused."""
    from theater.daemon.store import Store

    db_path = Path(repo) / ".theater" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    s = Store(db_path)
    try:
        registry = type("R", (), {"store": s})()
        spawner = Spawner(registry=registry)

        path, _branch = await spawner._spawn_named_worktree(
            root=repo, name="hijacked", base_branch=None
        )

        # Switch the checked-out branch in the worktree
        subprocess.run(  # noqa: ASYNC221
            ["git", "checkout", "-b", "some-other-branch"],
            cwd=path,
            check=True,
            capture_output=True,
        )

        with pytest.raises(BadRequest, match="checked out"):
            await spawner._spawn_named_worktree(root=repo, name="hijacked", base_branch=None)
    finally:
        s.close()


async def test_named_worktree_join_refused_unexpected_persisted_path(repo, store):
    from theater.daemon.store import Store

    db_path = Path(repo) / ".theater" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    s = Store(db_path)
    try:
        registry = type("R", (), {"store": s})()
        spawner = Spawner(registry=registry)

        await spawner._spawn_named_worktree(root=repo, name="expected", base_branch=None)
        other_path, other_branch = await spawner._spawn_named_worktree(
            root=repo, name="other", base_branch=None
        )
        s.upsert_named_worktree(
            repo_root=repo,
            name="expected",
            branch=other_branch,
            path=other_path,
            base_branch=None,
        )

        with pytest.raises(BadRequest, match="expected Theater-managed path"):
            await spawner._spawn_named_worktree(root=repo, name="expected", base_branch=None)
    finally:
        s.close()


async def test_named_worktree_join_refused_unexpected_persisted_branch(repo, store):
    from theater.daemon.store import Store

    db_path = Path(repo) / ".theater" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    s = Store(db_path)
    try:
        registry = type("R", (), {"store": s})()
        spawner = Spawner(registry=registry)

        path, _branch = await spawner._spawn_named_worktree(
            root=repo, name="expected-branch", base_branch=None
        )
        s.upsert_named_worktree(
            repo_root=repo,
            name="expected-branch",
            branch="other-branch",
            path=path,
            base_branch=None,
        )

        with pytest.raises(BadRequest, match="expected Theater-managed branch"):
            await spawner._spawn_named_worktree(root=repo, name="expected-branch", base_branch=None)
    finally:
        s.close()


async def test_named_worktree_is_retained_after_participants_die(repo, store):
    """Participant lifecycle never implicitly deletes a named workspace."""
    from theater.daemon.store import Store

    db_path = Path(repo) / ".theater" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    s = Store(db_path)
    try:
        registry = type("R", (), {"store": s})()
        spawner = Spawner(registry=registry)

        path, _branch = await spawner._spawn_named_worktree(
            root=repo, name="survive-kill", base_branch=None
        )

        p_a = _named_participant("child-a", path, "survive-kill")
        p_b = _named_participant("child-b", path, "survive-kill")
        s.upsert_participant(p_a)
        s.upsert_participant(p_b)

        s.set_status(p_a.id, "dead")
        s.set_status(p_b.id, "dead")

        assert Path(path).exists()

        # The named branch must survive the kill.
        verify = subprocess.run(  # noqa: ASYNC221
            ["git", "rev-parse", "--verify", "theater/named/survive-kill"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        assert verify.returncode == 0, "named shared branch must survive kill"

        row = s.get_named_worktree(repo_root=repo, name="survive-kill")
        assert row is not None
    finally:
        s.close()


async def test_named_worktree_name_remains_joinable_while_retained(repo, store):
    from theater.daemon.store import Store

    db_path = Path(repo) / ".theater" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    s = Store(db_path)
    try:
        registry = type("R", (), {"store": s})()
        spawner = Spawner(registry=registry)

        first = await spawner._spawn_named_worktree(root=repo, name="retained", base_branch=None)
        second = await spawner._spawn_named_worktree(root=repo, name="retained", base_branch=None)
        assert second == first
    finally:
        s.close()


async def test_named_worktree_missing_directory_requires_explicit_reconciliation(repo, store):
    from theater.daemon.store import Store

    db_path = Path(repo) / ".theater" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    s = Store(db_path)
    try:
        registry = type("R", (), {"store": s})()
        spawner = Spawner(registry=registry)

        path, _branch = await spawner._spawn_named_worktree(
            root=repo, name="missing-at-retire", base_branch=None
        )
        import shutil

        shutil.rmtree(path)

        assert s.get_named_worktree(repo_root=repo, name="missing-at-retire") is not None
        with pytest.raises(BadRequest, match="does not exist"):
            await spawner._spawn_named_worktree(
                root=repo, name="missing-at-retire", base_branch=None
            )
        verify = subprocess.run(  # noqa: ASYNC221
            ["git", "rev-parse", "--verify", "theater/named/missing-at-retire"],
            cwd=repo,
            capture_output=True,
            check=False,
        )
        assert verify.returncode == 0
    finally:
        s.close()


async def test_launch_failure_preserves_unique_worktree(repo, monkeypatch):
    """A failed launch reservation leaves its worktree for explicit cleanup."""
    import theater.daemon.spawning.service as spawner_mod
    from theater.daemon.spawning.models import SpawnRequest
    from theater.daemon.spawning.service import Spawner
    from theater.models import Status

    monkeypatch.setattr(spawner_mod.shutil, "which", lambda b: f"/usr/bin/{b}")

    from theater.daemon.store import Store

    db_path = Path(repo) / ".theater" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    s = Store(db_path)
    try:
        registry = type("R", (), {"store": s})()
        from theater.daemon.registry import Registry

        registry = Registry(s)
        spawner = Spawner(registry)

        req = SpawnRequest(
            harness="vibe",
            prompt="say hello",
            cwd=repo,
            approval="edits",
            worktree=True,
        )
        reservation = await spawner.reserve(req)
        child_id = reservation.participant.id
        wt_path = wt.worktree_path(repo, child_id)
        assert Path(wt_path).exists(), "worktree must exist after reserve"

        spawner._preserve_failed_launch(reservation.participant)

        assert Path(wt_path).exists(), f"worktree should be retained: {wt_path}"

        # The branch must be deleted.
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", "rev-parse", "--verify", wt.branch_name(child_id)],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0, "branch should be retained"

        # The participant must be DEAD.
        p = registry.get(child_id)
        assert p is not None
        assert p.status is Status.DEAD
    finally:
        s.close()


async def test_launch_failure_preserves_named_worktree(repo, monkeypatch):
    """A named worktree survives once terminal dispatch may have begun.

    The exact named record remains discoverable for later explicit cleanup.
    """
    import theater.daemon.spawning.service as spawner_mod
    from theater.daemon.spawning.models import SpawnRequest
    from theater.daemon.spawning.service import Spawner
    from theater.models import Status

    monkeypatch.setattr(spawner_mod.shutil, "which", lambda b: f"/usr/bin/{b}")

    from theater.daemon.store import Store

    db_path = Path(repo) / ".theater" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    s = Store(db_path)
    try:
        from theater.daemon.registry import Registry

        registry = Registry(s)
        spawner = Spawner(registry)

        name = "phase3-named"
        req = SpawnRequest(
            harness="vibe",
            prompt="say hello",
            cwd=repo,
            approval="edits",
            worktree=name,
        )
        reservation = await spawner.reserve(req)
        child_id = reservation.participant.id
        wt_path = wt.named_worktree_path(repo, name)
        assert Path(wt_path).exists(), "named worktree must exist after reserve"

        spawner._preserve_failed_launch(reservation.participant)

        assert Path(wt_path).exists(), f"named worktree dir should be retained: {wt_path}"

        # The named branch is always retained.
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", "rev-parse", "--verify", wt.named_branch_name(name)],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0, "named branch must be retained"

        # The named-worktree row preserves the exact path and branch.
        row = s.get_named_worktree(repo_root=repo, name=name)
        assert row is not None, "named-worktree row should be retained"

        # The participant must be DEAD.
        p = registry.get(child_id)
        assert p is not None
        assert p.status is Status.DEAD
    finally:
        s.close()


async def test_named_worktree_lock_serializes_concurrent_creates(repo, store):
    """Two concurrent ``_spawn_named_worktree`` calls for the same name and
    repo must be serialized — the lock prevents the create/create race where
    both see no row, both call ``git worktree add``, and one fails."""
    from theater.daemon.spawning.service import Spawner

    registry = type("R", (), {"store": store})()
    spawner = Spawner(registry=registry)

    task1 = asyncio.create_task(
        spawner._spawn_named_worktree(root=repo, name="shared", base_branch=None)
    )
    task2 = asyncio.create_task(
        spawner._spawn_named_worktree(root=repo, name="shared", base_branch=None)
    )
    path1, branch1 = await task1
    path2, branch2 = await task2

    assert path1 == path2
    assert branch1 == branch2

    row = store.get_named_worktree(repo_root=repo, name="shared")
    assert row is not None
    assert row["path"] == path1


async def test_named_worktree_cancel_during_create_reconciles_state(repo, store):
    """Cancelling during ``create_named_worktree`` must still commit the
    store row (reconcile) and propagate ``CancelledError``."""
    import theater.daemon.worktree as wt_mod
    from theater.daemon.spawning.service import Spawner

    registry = type("R", (), {"store": store})()
    spawner = Spawner(registry=registry)

    create_done = asyncio.Event()
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        if "worktree" in argv and "add" in argv:
            r = real_run(argv, **kwargs)
            create_done.set()
            return r
        return real_run(argv, **kwargs)

    import unittest.mock

    with unittest.mock.patch.object(wt_mod.subprocess, "run", fake_run):
        task = asyncio.create_task(
            spawner._spawn_named_worktree(root=repo, name="cancel-test", base_branch=None)
        )
        await asyncio.wait_for(create_done.wait(), timeout=5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    row = store.get_named_worktree(repo_root=repo, name="cancel-test")
    assert row is not None, "store row must be committed even after cancellation"
    assert Path(row["path"]).exists()
