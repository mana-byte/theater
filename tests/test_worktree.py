"""Tests for git worktree management.

Uses a real git repo in a temp directory — the worktree module shells out
to `git`, so the tests must too. No mocking; the tests are fast because
the repos are tiny (one commit, one file).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from theater.daemon import worktree as wt
from theater.daemon.spawner import Spawner
from theater.models import BadRequest


@pytest.fixture
def repo(tmp_path):
    """A real git repo with one commit."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("# test repo\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    return str(root)


def test_is_git_repo(repo):
    assert wt.is_git_repo(repo) is True


def test_is_not_git_repo(tmp_path):
    d = tmp_path / "not-a-repo"
    d.mkdir()
    assert wt.is_git_repo(str(d)) is False


def test_repo_root(repo):
    assert wt.repo_root(repo) == repo


def test_worktree_path(repo):
    path = wt.worktree_path(repo, "abc123")
    assert path == f"{repo}/.theater/worktrees/abc123"


def test_create_worktree(repo):
    """Creating a worktree gives an isolated working directory."""
    wt_path = wt.create_worktree(repo_root=repo, child_id="child1")
    assert Path(wt_path).exists()
    assert Path(wt_path, ".git").exists()
    # The worktree has the committed file
    assert (Path(wt_path) / "README.md").exists()
    # The branch exists
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "theater/child1"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0


def test_create_worktree_isolates_commits(repo):
    """A commit in the worktree does not appear in the main repo's HEAD."""
    wt_path = wt.create_worktree(repo_root=repo, child_id="child2")
    # Make a commit in the worktree
    (Path(wt_path) / "new.txt").write_text("new file\n")
    subprocess.run(["git", "add", "."], cwd=wt_path, check=True)
    subprocess.run(["git", "commit", "-m", "new"], cwd=wt_path, check=True, capture_output=True)
    # The main repo does not have new.txt
    assert not (Path(repo) / "new.txt").exists()
    # The worktree does
    assert (Path(wt_path) / "new.txt").exists()


def test_create_worktree_duplicate_branch_rejected(repo):
    """Creating a worktree with an existing branch name is rejected."""
    wt.create_worktree(repo_root=repo, child_id="dup")
    with pytest.raises(BadRequest):
        wt.create_worktree(repo_root=repo, child_id="dup")


def test_create_worktree_with_base_branch(repo):
    """Creating a worktree from a specific base branch works."""
    # Create a branch with a different commit
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True, capture_output=True)
    (Path(repo) / "feature.txt").write_text("feature\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "feature"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)

    wt_path = wt.create_worktree(repo_root=repo, child_id="child3", base_branch="feature")
    # The worktree has the feature file
    assert (Path(wt_path) / "feature.txt").exists()


def test_remove_worktree(repo):
    """Removing a worktree deletes the directory and branch."""
    wt_path = wt.create_worktree(repo_root=repo, child_id="child4")
    assert Path(wt_path).exists()

    result = wt.remove_worktree(repo_root=repo, child_id="child4")

    assert result.ok
    assert not Path(wt_path).exists()
    # The branch is gone
    verify = subprocess.run(
        ["git", "rev-parse", "--verify", "theater/child4"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode != 0


def test_remove_worktree_with_uncommitted_changes(repo):
    """Removing a worktree with uncommitted changes uses --force."""
    wt_path = wt.create_worktree(repo_root=repo, child_id="child5")
    (Path(wt_path) / "uncommitted.txt").write_text("dirty\n")
    # Should not raise
    result = wt.remove_worktree(repo_root=repo, child_id="child5")
    assert result.ok
    assert not Path(wt_path).exists()


def test_main_repo_root_from_inside_worktree(repo):
    """main_repo_root returns the *main* root, not the worktree's top level.

    This is the core of the bug: the caller derives repo_root from the
    child's cwd via repo_root(), which for a worktree child returns the
    worktree path. main_repo_root must see through that to the shared root.
    """
    wt_path = wt.create_worktree(repo_root=repo, child_id="wtroot1")

    # repo_root (the buggy caller) returns the worktree's own top level.
    wrong_root = wt.repo_root(wt_path)
    assert wrong_root == wt_path

    # main_repo_root returns the actual main repo root.
    right_root = wt.main_repo_root(wt_path)
    assert right_root is not None
    # Resolve both for comparison (macOS may canonicalise paths).
    assert Path(right_root).resolve() == Path(repo).resolve()


def test_main_repo_root_fallback_when_dir_gone(repo):
    """When the worktree directory is deleted, main_repo_root falls back
    to stripping the .theater/worktrees/<id> suffix."""
    wt_path = wt.create_worktree(repo_root=repo, child_id="wtroot2")
    child_id = "wtroot2"

    # Simulate the directory being gone (as happens when a worktree is
    # deleted out from under us, or the child's cwd is stale).
    import shutil

    shutil.rmtree(wt_path)

    # git rev-parse will fail because the cwd no longer exists. The
    # fallback strips the suffix to recover the main repo root.
    result = wt.main_repo_root(wt_path, child_id=child_id)
    assert result is not None
    assert Path(result).resolve() == Path(repo).resolve()


def test_remove_worktree_with_wrong_root_from_caller(repo):
    """remove_worktree must succeed even when repo_root is the worktree
    path (the buggy caller's derivation), not the main repo root.

    This is the test that would have caught the original bug: the caller
    derives repo_root from inside the child's worktree cwd, which
    repo_root() returns as the worktree path. remove_worktree must
    internally re-derive the true main root.
    """
    wt_path = wt.create_worktree(repo_root=repo, child_id="bugrepro1")
    assert Path(wt_path).exists()

    # Derive repo_root the way the buggy caller does: from inside the
    # worktree. This returns the worktree's own top level, not the main
    # repo root.
    wrong_root = wt.repo_root(wt_path)
    assert wrong_root == wt_path  # the bug

    # remove_worktree must handle this and still clean up correctly.
    result = wt.remove_worktree(repo_root=wrong_root, child_id="bugrepro1")

    assert result.ok
    assert result.worktree_removed
    assert result.branch_removed
    assert not Path(wt_path).exists()
    # Branch genuinely gone
    verify = subprocess.run(
        ["git", "rev-parse", "--verify", "theater/bugrepro1"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode != 0
    # No stale worktree list entries
    list_result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "bugrepro1" not in list_result.stdout


def test_remove_worktree_already_deleted_directory(repo):
    """remove_worktree handles the case where the worktree directory was
    already deleted (e.g. by rm -rf or a prior partial removal).

    git worktree remove fails with 'not a working tree', but we must
    prune the stale admin record and still delete the branch. The
    original code silently failed here too.
    """
    import shutil

    wt_path = wt.create_worktree(repo_root=repo, child_id="bugrepro2")
    assert Path(wt_path).exists()

    # Delete the directory out from under git, leaving a stale admin
    # record. This simulates a crash or manual cleanup that removed the
    # directory but not the git metadata.
    shutil.rmtree(wt_path)
    assert not Path(wt_path).exists()

    # The branch still exists and the worktree admin record is stale.
    verify_before = subprocess.run(
        ["git", "rev-parse", "--verify", "theater/bugrepro2"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify_before.returncode == 0  # branch still there

    result = wt.remove_worktree(repo_root=repo, child_id="bugrepro2")

    assert result.ok
    assert result.branch_removed
    # Branch genuinely gone
    verify_after = subprocess.run(
        ["git", "rev-parse", "--verify", "theater/bugrepro2"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify_after.returncode != 0
    # No stale worktree list entries
    list_result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "bugrepro2" not in list_result.stdout


def test_remove_worktree_keeps_the_branch_when_asked(repo):
    """delete_branch=False prunes the directory and leaves the branch.

    This is the self-exit policy: a child that ended on its own usually
    ended because it finished, and the branch is the only handle on
    whatever it committed. Reclaiming the directory must not throw the
    commits away with it.
    """
    wt_path = wt.create_worktree(repo_root=repo, child_id="keepbranch")
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "child work"],
        cwd=wt_path,
        check=True,
        capture_output=True,
    )

    result = wt.remove_worktree(repo_root=repo, child_id="keepbranch", delete_branch=False)

    # ok reflects the directory alone; nothing was asked of the branch,
    # so branch_removed stays False rather than claiming a success.
    assert result.ok
    assert result.worktree_removed
    assert not result.branch_removed
    assert not Path(wt_path).exists()

    verify = subprocess.run(
        ["git", "rev-parse", "--verify", "theater/keepbranch"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 0, "the child's branch must survive"


# ---- Spawner.retire ----------------------------------------------------
#
# retire() is the single door every death path goes through, and what it
# has to get right is a git question, not a daemon question: it must
# derive the main repo root from a cwd that *is* a worktree. So it is
# tested here, against the same real repo fixture, rather than against a
# mocked spawner elsewhere.


def _participant(child_id: str, cwd: str):
    from theater.models import Participant, Tier

    return Participant(
        id=child_id,
        harness="vibe",
        tier=Tier.SPAWNED,
        cwd=cwd,
        branch=wt.branch_name(child_id),
    )


def _spawner():
    from theater.daemon.spawner import Spawner

    # retire() never touches the registry; passing None keeps the test
    # to the one behaviour it is about.
    return Spawner(registry=None)


async def test_retire_removes_worktree_and_branch_of_a_killed_child(repo):
    """The kill path, end to end, with the cwd shape production has.

    The child's cwd is its worktree — the exact input that made every
    real removal fail before, because deriving the repo root from it
    with `--show-toplevel` yields the worktree itself.
    """
    wt_path = wt.create_worktree(repo_root=repo, child_id="killme")
    p = _participant("killme", wt_path)

    await _spawner().retire(p, delete_branch=True)

    assert not Path(wt_path).exists()
    verify = subprocess.run(  # noqa: ASYNC221
        ["git", "rev-parse", "--verify", "theater/killme"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode != 0
    listing = subprocess.run(  # noqa: ASYNC221
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "killme" not in listing.stdout


async def test_retire_keeps_the_branch_of_a_child_that_exited(repo):
    """The reaper path: directory reclaimed, commits preserved."""
    wt_path = wt.create_worktree(repo_root=repo, child_id="exited")
    subprocess.run(  # noqa: ASYNC221
        ["git", "commit", "--allow-empty", "-m", "child work"],
        cwd=wt_path,
        check=True,
        capture_output=True,
    )
    p = _participant("exited", wt_path)

    await _spawner().retire(p, delete_branch=False)

    assert not Path(wt_path).exists()
    verify = subprocess.run(  # noqa: ASYNC221
        ["git", "rev-parse", "--verify", "theater/exited"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 0


async def test_retire_ignores_a_participant_without_a_theater_branch(repo):
    """A child spawned without worktree=True shares the parent's checkout.

    Its cwd is the repo itself and its branch is whatever the user is on.
    Retiring it must touch nothing: the alternative is a daemon that
    deletes the branch a human is working on.
    """
    from theater.models import Participant, Tier

    p = Participant(id="plain", harness="vibe", tier=Tier.SPAWNED, cwd=repo, branch="main")

    await _spawner().retire(p, delete_branch=True)

    verify = subprocess.run(  # noqa: ASYNC221
        ["git", "rev-parse", "--verify", "main"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 0
    assert Path(repo, "README.md").exists()


# ---- named worktree name validation -----------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "-foo",
        "foo/bar",
        "..",
        ".",
        "foo/../bar",
        "main",
        "HEAD",
        "a" * 101,
    ],
)
def test_validate_name_rejects_bad_names(name):
    with pytest.raises(BadRequest):
        wt.validate_name(name)


@pytest.mark.parametrize(
    "name",
    ["my-team", "feature_x", "bugfix-2", "shared.thing", "alpha"],
)
def test_validate_name_accepts_good_names(name):
    wt.validate_name(name)


# ---- named worktree creation and removal -------------------------------


def test_named_worktree_path(repo):
    path = wt.named_worktree_path(repo, "shared")
    assert path == f"{repo}/.theater/worktrees/named/shared"


def test_named_branch_name():
    assert wt.named_branch_name("shared") == "theater/named/shared"


def test_create_named_worktree(repo):
    """Creating a named worktree gives an isolated directory."""
    wt_path, branch = wt.create_named_worktree(repo_root=repo, name="feature-a")
    assert Path(wt_path).exists()
    assert Path(wt_path, ".git").exists()
    assert (Path(wt_path) / "README.md").exists()
    assert branch == "theater/named/feature-a"
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "theater/named/feature-a"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0


def test_create_named_worktree_with_base_branch(repo):
    """Creating a named worktree from a specific base branch works."""
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True, capture_output=True)
    (Path(repo) / "feature.txt").write_text("feature\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "feature"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)

    wt_path, branch = wt.create_named_worktree(repo_root=repo, name="child3", base_branch="feature")
    assert (Path(wt_path) / "feature.txt").exists()
    assert branch == "theater/named/child3"


def test_create_named_worktree_duplicate_rejected(repo):
    """Creating a named worktree with an existing branch name is rejected."""
    wt.create_named_worktree(repo_root=repo, name="dup")
    with pytest.raises(BadRequest):
        wt.create_named_worktree(repo_root=repo, name="dup")


def test_remove_named_worktree(repo):
    """Removing a named worktree deletes the directory and branch."""
    wt_path, _ = wt.create_named_worktree(repo_root=repo, name="removeme")
    assert Path(wt_path).exists()

    result = wt.remove_named_worktree(repo_root=repo, name="removeme")

    assert result.ok
    assert not Path(wt_path).exists()
    verify = subprocess.run(
        ["git", "rev-parse", "--verify", "theater/named/removeme"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode != 0


def test_remove_named_worktree_keeps_branch_when_asked(repo):
    """delete_branch=False prunes the directory and leaves the branch."""
    wt_path, _ = wt.create_named_worktree(repo_root=repo, name="keepbranch-named")
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "shared work"],
        cwd=wt_path,
        check=True,
        capture_output=True,
    )

    result = wt.remove_named_worktree(repo_root=repo, name="keepbranch-named", delete_branch=False)

    assert result.ok
    assert result.worktree_removed
    assert not result.branch_removed
    assert not Path(wt_path).exists()
    verify = subprocess.run(
        ["git", "rev-parse", "--verify", "theater/named/keepbranch-named"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 0, "the named worktree's branch must survive"


# ---- named worktree spawner create-or-join ----------------------------


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


async def test_named_worktree_retire_does_not_remove_when_others_live(repo, store):
    """Retiring one participant in a shared named worktree must not remove
    the directory or branch while another live participant is still using it."""
    from theater.daemon.store import Store

    db_path = Path(repo) / ".theater" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    s = Store(db_path)
    try:
        registry = type("R", (), {"store": s})()
        spawner = Spawner(registry=registry)

        path, _branch = await spawner._spawn_named_worktree(
            root=repo, name="shared-retire", base_branch=None
        )

        p1 = _named_participant("child-a", path, "shared-retire")
        p2 = _named_participant("child-b", path, "shared-retire")
        s.upsert_participant(p1)
        s.upsert_participant(p2)

        # Retire p1 — p2 is still live in the same cwd
        await spawner.retire(p1, delete_branch=True)

        # The directory and branch must still exist
        assert Path(path).exists()
        verify = subprocess.run(  # noqa: ASYNC221
            ["git", "rev-parse", "--verify", "theater/named/shared-retire"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        assert verify.returncode == 0

        # The named_worktrees row must still exist
        row = s.get_named_worktree(repo_root=repo, name="shared-retire")
        assert row is not None
    finally:
        s.close()


async def test_named_worktree_retire_removes_when_last_participant(repo, store):
    """Retiring the last live participant in a named worktree removes the
    directory but always retains the shared branch."""
    from theater.daemon.store import Store

    db_path = Path(repo) / ".theater" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    s = Store(db_path)
    try:
        registry = type("R", (), {"store": s})()
        spawner = Spawner(registry=registry)

        path, _branch = await spawner._spawn_named_worktree(
            root=repo, name="last-one", base_branch=None
        )

        p1 = _named_participant("only-child", path, "last-one")
        s.upsert_participant(p1)

        await spawner.retire(p1, delete_branch=True)

        assert not Path(path).exists()
        verify = subprocess.run(  # noqa: ASYNC221
            ["git", "rev-parse", "--verify", "theater/named/last-one"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        assert verify.returncode == 0, "the named shared branch must survive teardown"

        # The named_worktrees row should be gone
        row = s.get_named_worktree(repo_root=repo, name="last-one")
        assert row is None
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


# ---- Fix 1: canonical scope from inside a linked worktree ---------------


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


# ---- Fix 2: base_branch rejection on join -------------------------------


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


# ---- Fix 3: stale registry rows ----------------------------------------


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


# ---- Fix 4: named branch survives kill ---------------------------------


async def test_named_worktree_branch_survives_kill(repo, store):
    """A named shared branch must never be auto-deleted on kill.

    Participant A finishes, B is the last live member and is killed.
    The directory is removed but the shared branch survives.
    """
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

        # A finishes first — retire and mark dead (full teardown for a
        # self-exit uses delete_branch=False). B is still live.
        await spawner.retire(p_a, delete_branch=False)
        s.set_status(p_a.id, "dead")

        # Directory still exists (B is live)
        assert Path(path).exists()

        # B is killed — last live member, delete_branch=True
        await spawner.retire(p_b, delete_branch=True)
        s.set_status(p_b.id, "dead")

        # Directory removed
        assert not Path(path).exists()

        # Branch must survive — this is the core of Fix 4
        verify = subprocess.run(  # noqa: ASYNC221
            ["git", "rev-parse", "--verify", "theater/named/survive-kill"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        assert verify.returncode == 0, "named shared branch must survive kill"

        # The named_worktrees row should be gone
        row = s.get_named_worktree(repo_root=repo, name="survive-kill")
        assert row is None
    finally:
        s.close()


async def test_named_worktree_name_not_recreatable_after_teardown(repo, store):
    """After last teardown, the branch remains, so the name cannot be recreated."""
    from theater.daemon.store import Store

    db_path = Path(repo) / ".theater" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    s = Store(db_path)
    try:
        registry = type("R", (), {"store": s})()
        spawner = Spawner(registry=registry)

        await spawner._spawn_named_worktree(root=repo, name="retained", base_branch=None)
        p = _named_participant("only", wt.named_worktree_path(repo, "retained"), "retained")
        s.upsert_participant(p)
        await spawner.retire(p, delete_branch=True)

        # The row is gone, but the branch exists — creating again should fail
        # because create_named_worktree checks if the branch already exists
        with pytest.raises(BadRequest, match="already exists"):
            await spawner._spawn_named_worktree(root=repo, name="retained", base_branch=None)
    finally:
        s.close()


async def test_named_worktree_retire_recovers_when_directory_already_missing(repo, store):
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
        participant = _named_participant("only", path, "missing-at-retire")
        s.upsert_participant(participant)

        import shutil

        shutil.rmtree(path)
        await spawner.retire(participant, delete_branch=True)

        assert s.get_named_worktree(repo_root=repo, name="missing-at-retire") is None
        verify = subprocess.run(  # noqa: ASYNC221
            ["git", "rev-parse", "--verify", "theater/named/missing-at-retire"],
            cwd=repo,
            capture_output=True,
            check=False,
        )
        assert verify.returncode == 0
    finally:
        s.close()


# ---- Fix 6: runtime type validation ------------------------------------


def test_validate_worktree_param_rejects_int():
    from theater.daemon.methods import _validate_worktree_param
    from theater.models import BadRequest

    with pytest.raises(BadRequest, match="must be bool, str, or None"):
        _validate_worktree_param(1)


def test_validate_worktree_param_rejects_list():
    from theater.daemon.methods import _validate_worktree_param
    from theater.models import BadRequest

    with pytest.raises(BadRequest, match="must be bool, str, or None"):
        _validate_worktree_param(["name"])


def test_validate_worktree_param_rejects_empty_string():
    from theater.daemon.methods import _validate_worktree_param
    from theater.models import BadRequest

    with pytest.raises(BadRequest, match="non-empty string"):
        _validate_worktree_param("")


def test_validate_worktree_param_accepts_true():
    from theater.daemon.methods import _validate_worktree_param

    assert _validate_worktree_param(True) is True


def test_validate_worktree_param_accepts_false():
    from theater.daemon.methods import _validate_worktree_param

    assert _validate_worktree_param(False) is False


def test_validate_worktree_param_accepts_none():
    from theater.daemon.methods import _validate_worktree_param

    assert _validate_worktree_param(None) is False


def test_validate_worktree_param_accepts_string():
    from theater.daemon.methods import _validate_worktree_param

    assert _validate_worktree_param("my-name") == "my-name"


# ---- Fix 7: git check-ref-format validation ---------------------------


@pytest.mark.parametrize(
    "name",
    ["trailing.", "double..dot", "ends.lock", "has..double"],
)
def test_validate_name_rejects_invalid_git_refs(repo, name):
    """Names that produce invalid git refs are rejected by check-ref-format."""
    with pytest.raises(BadRequest):
        wt.validate_name(name)


# ---- phase 3: reserve/launch worktree cleanup ------------------------


async def test_reserve_then_launch_failure_retires_unique_worktree(repo, monkeypatch):
    """A unique worktree created during reserve is retired when launch fails.

    The reserve/launch split means the worktree exists before the pane. If
    launch raises, ``cleanup_reservation`` must retire the worktree (remove
    the directory and delete the branch) and mark the participant DEAD.
    """
    import theater.daemon.spawner as spawner_mod
    from theater.daemon.spawner import Spawner, SpawnRequest
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

        # Sabotage tmux.new_window so launch fails.
        async def boom(**kwargs):
            raise RuntimeError("tmux exploded")

        monkeypatch.setattr(spawner_mod.tmux, "new_window", boom)

        with pytest.raises(RuntimeError, match="tmux exploded"):
            await spawner.launch(reservation)

        # The worktree directory must be gone.
        assert not Path(wt_path).exists(), f"worktree should be gone: {wt_path}"

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
        assert result.returncode != 0, "branch should be deleted"

        # The participant must be DEAD.
        p = registry.get(child_id)
        assert p is not None
        assert p.status is Status.DEAD
    finally:
        s.close()


async def test_reserve_then_launch_failure_retires_named_worktree(repo, monkeypatch):
    """A named worktree created during reserve is retired when launch fails.

    Named worktree semantics: the directory is removed but the branch is
    retained (other participants may have completed work on it).
    """
    import theater.daemon.spawner as spawner_mod
    from theater.daemon.spawner import Spawner, SpawnRequest
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

        async def boom(**kwargs):
            raise RuntimeError("tmux exploded")

        monkeypatch.setattr(spawner_mod.tmux, "new_window", boom)

        with pytest.raises(RuntimeError, match="tmux exploded"):
            await spawner.launch(reservation)

        # The worktree directory must be gone (last live participant).
        assert not Path(wt_path).exists(), f"named worktree dir should be gone: {wt_path}"

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

        # The named-worktree row must be deleted (directory was removed).
        row = s.get_named_worktree(repo_root=repo, name=name)
        assert row is None, "named-worktree row should be deleted"

        # The participant must be DEAD.
        p = registry.get(child_id)
        assert p is not None
        assert p.status is Status.DEAD
    finally:
        s.close()


# ---- _git() timeout and error handling -------------------------------------


def test_git_returns_failed_result_on_timeout(monkeypatch):
    """_git() must not raise TimeoutExpired — it synthesizes a failed
    CompletedProcess so callers' existing ``returncode != 0`` branching
    handles the failure.  This is the fix for the incident: a stuck
    ``git worktree remove`` blocked the event loop and the reaper retried
    forever because TimeoutExpired propagated past retire().
    """
    import theater.daemon.worktree as wt_mod

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 10))

    monkeypatch.setattr(wt_mod.subprocess, "run", fake_run)

    result = wt._git(
        ["git", "rev-parse", "--show-toplevel"],
        cwd="/tmp",
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 124
    assert "timed out" in result.stderr


def test_git_returns_failed_result_on_oserror(monkeypatch):
    """_git() must not raise OSError (missing binary) — synthesizes rc=127."""
    import theater.daemon.worktree as wt_mod

    def fake_run(argv, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(wt_mod.subprocess, "run", fake_run)

    result = wt._git(
        ["git", "rev-parse", "--show-toplevel"],
        cwd="/tmp",
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 127
    assert "git not found" in result.stderr


async def test_remove_worktree_returns_ok_false_on_git_timeout(monkeypatch, repo):
    """remove_worktree must return WorktreeRemoveResult(ok=False) when git
    times out, not raise — the docstring says "never raises on git failure"
    and the reaper depends on this."""
    import theater.daemon.worktree as wt_mod

    # Let create_worktree succeed (real git), but make remove fail.
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        if "worktree" in argv and "remove" in argv:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 10))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(wt_mod.subprocess, "run", fake_run)

    wt_path = wt.create_worktree(repo_root=repo, child_id="timeout-test")
    result = wt.remove_worktree(repo_root=repo, child_id="timeout-test", delete_branch=True)

    assert result.ok is False
    assert result.worktree_removed is False
    assert Path(wt_path).exists()


# ---- timeout-as-indeterminate: rc 124/127 must NOT be treated as "gone" ----


def test_remove_worktree_timeout_does_not_falsely_report_branch_removed(monkeypatch, repo):
    """When ``git branch -D`` fails and the verify ``rev-parse`` times out
    (rc=124), ``remove_worktree`` must NOT set ``branch_removed=True`` — a
    timeout does not prove the branch is gone."""
    import theater.daemon.worktree as wt_mod

    wt.create_worktree(repo_root=repo, child_id="indeterminate-test")
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        if "branch" in argv and "-D" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout="", stderr="error: branch delete failed"
            )
        if "rev-parse" in argv and "--verify" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=124, stdout="", stderr="git timed out"
            )
        return real_run(argv, **kwargs)

    monkeypatch.setattr(wt_mod.subprocess, "run", fake_run)

    result = wt.remove_worktree(repo_root=repo, child_id="indeterminate-test", delete_branch=True)

    assert result.branch_removed is False, "timeout (rc=124) must NOT be treated as 'branch gone'"
    assert result.ok is False
    assert any("indeterminate" in e for e in result.errors)


def test_remove_worktree_missing_git_does_not_falsely_report_branch_removed(monkeypatch, repo):
    """When verify rev-parse returns rc=127 (missing git binary),
    ``remove_worktree`` must NOT set ``branch_removed=True``."""
    import theater.daemon.worktree as wt_mod

    wt.create_worktree(repo_root=repo, child_id="missing-git-test")
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        if "branch" in argv and "-D" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout="", stderr="error: branch delete failed"
            )
        if "rev-parse" in argv and "--verify" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=127, stdout="", stderr="git: not found"
            )
        return real_run(argv, **kwargs)

    monkeypatch.setattr(wt_mod.subprocess, "run", fake_run)

    result = wt.remove_worktree(repo_root=repo, child_id="missing-git-test", delete_branch=True)

    assert result.branch_removed is False, (
        "missing git (rc=127) must NOT be treated as 'branch gone'"
    )
    assert result.ok is False


def test_remove_worktree_git_fatal_128_does_not_falsely_report_branch_removed(monkeypatch, repo):
    """When verify rev-parse returns rc=128 (git's actual code for a
    missing ref OR a fatal error), ``remove_worktree`` must NOT set
    ``branch_removed=True`` — 128 is indeterminate, not 'gone'."""
    import theater.daemon.worktree as wt_mod

    wt.create_worktree(repo_root=repo, child_id="fatal-128-test")
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        if "branch" in argv and "-D" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout="", stderr="error: branch delete failed"
            )
        if "rev-parse" in argv and "--verify" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=128, stdout="", stderr="fatal: not a valid ref"
            )
        return real_run(argv, **kwargs)

    monkeypatch.setattr(wt_mod.subprocess, "run", fake_run)

    result = wt.remove_worktree(repo_root=repo, child_id="fatal-128-test", delete_branch=True)

    assert result.branch_removed is False, "git fatal rc=128 must NOT be treated as 'branch gone'"
    assert result.ok is False


def test_remove_worktree_genuine_rc1_still_reports_branch_removed(monkeypatch, repo):
    """When ``git branch -D`` succeeds (rc=0), ``remove_worktree`` sets
    ``branch_removed=True`` — that is the normal success path, and the
    indeterminate fix must not break it."""
    import theater.daemon.worktree as wt_mod

    wt.create_worktree(repo_root=repo, child_id="genuine-gone-test")
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        if "branch" in argv and "-D" in argv:
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(wt_mod.subprocess, "run", fake_run)

    result = wt.remove_worktree(repo_root=repo, child_id="genuine-gone-test", delete_branch=True)

    assert result.branch_removed is True, "git branch -D rc=0 must report branch_removed=True"
    assert result.ok is True


# ---- named-worktree serialization -------------------------------------------


async def test_named_worktree_lock_serializes_concurrent_creates(repo, store):
    """Two concurrent ``_spawn_named_worktree`` calls for the same name and
    repo must be serialized — the lock prevents the create/create race where
    both see no row, both call ``git worktree add``, and one fails."""
    from theater.daemon.spawner import Spawner

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
    from theater.daemon.spawner import Spawner

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
