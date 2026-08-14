"""Tests for git worktree management.

Uses a real git repo in a temp directory — the worktree module shells out
to `git`, so the tests must too. No mocking; the tests are fast because
the repos are tiny (one commit, one file).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from theater.daemon import worktree as wt
from theater.models import BadRequest


@pytest.fixture
def repo(tmp_path):
    """A real git repo with one commit."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("# test repo\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True,
                   capture_output=True)
    return str(root)


def test_is_git_repo(repo):
    assert wt.is_git_repo(repo) is True


def test_is_not_git_repo(tmp_path):
    d = tmp_path / "not-a-repo"
    d.mkdir()
    assert wt.is_git_repo(str(d)) is False


def test_repo_root(repo):
    assert wt.repo_root(repo) == repo


def test_branch_name():
    assert wt.branch_name("abc123") == "theater/abc123"


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
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0


def test_create_worktree_isolates_commits(repo):
    """A commit in the worktree does not appear in the main repo's HEAD."""
    wt_path = wt.create_worktree(repo_root=repo, child_id="child2")
    # Make a commit in the worktree
    (Path(wt_path) / "new.txt").write_text("new file\n")
    subprocess.run(["git", "add", "."], cwd=wt_path, check=True)
    subprocess.run(["git", "commit", "-m", "new"], cwd=wt_path, check=True,
                   capture_output=True)
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
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True,
                   capture_output=True)
    (Path(repo) / "feature.txt").write_text("feature\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "feature"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True,
                   capture_output=True)

    wt_path = wt.create_worktree(
        repo_root=repo, child_id="child3", base_branch="feature"
    )
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
        cwd=repo, capture_output=True, text=True, check=False,
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
    result = wt.remove_worktree(
        repo_root=wrong_root, child_id="bugrepro1"
    )

    assert result.ok
    assert result.worktree_removed
    assert result.branch_removed
    assert not Path(wt_path).exists()
    # Branch genuinely gone
    verify = subprocess.run(
        ["git", "rev-parse", "--verify", "theater/bugrepro1"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert verify.returncode != 0
    # No stale worktree list entries
    list_result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo, capture_output=True, text=True, check=False,
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
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert verify_before.returncode == 0  # branch still there

    result = wt.remove_worktree(
        repo_root=repo, child_id="bugrepro2"
    )

    assert result.ok
    assert result.branch_removed
    # Branch genuinely gone
    verify_after = subprocess.run(
        ["git", "rev-parse", "--verify", "theater/bugrepro2"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert verify_after.returncode != 0
    # No stale worktree list entries
    list_result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert "bugrepro2" not in list_result.stdout


def test_remove_worktree_already_deleted_with_wrong_root(repo):
    """The already-deleted case must also work when the caller passes
    the worktree path as repo_root (the buggy derivation). The fallback
    in main_repo_root strips the suffix to recover the main root.
    """
    import shutil

    wt_path = wt.create_worktree(repo_root=repo, child_id="bugrepro3")
    shutil.rmtree(wt_path)

    # Derive root the buggy way: from the now-deleted worktree path.
    # repo_root() will fail (cwd doesn't exist), but we pass the path
    # directly as repo_root — which is the worktree path.
    wrong_root = wt_path  # the worktree path itself

    result = wt.remove_worktree(
        repo_root=wrong_root, child_id="bugrepro3"
    )

    assert result.ok
    assert result.branch_removed
    verify = subprocess.run(
        ["git", "rev-parse", "--verify", "theater/bugrepro3"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert verify.returncode != 0


def test_remove_worktree_keeps_the_branch_when_asked(repo):
    """delete_branch=False prunes the directory and leaves the branch.

    This is the self-exit policy: a child that ended on its own usually
    ended because it finished, and the branch is the only handle on
    whatever it committed. Reclaiming the directory must not throw the
    commits away with it.
    """
    wt_path = wt.create_worktree(repo_root=repo, child_id="keepbranch")
    subprocess.run(["git", "commit", "--allow-empty", "-m", "child work"],
                   cwd=wt_path, check=True, capture_output=True)

    result = wt.remove_worktree(
        repo_root=repo, child_id="keepbranch", delete_branch=False
    )

    # ok reflects the directory alone; nothing was asked of the branch,
    # so branch_removed stays False rather than claiming a success.
    assert result.ok
    assert result.worktree_removed
    assert not result.branch_removed
    assert not Path(wt_path).exists()

    verify = subprocess.run(
        ["git", "rev-parse", "--verify", "theater/keepbranch"],
        cwd=repo, capture_output=True, text=True, check=False,
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


def test_retire_removes_worktree_and_branch_of_a_killed_child(repo):
    """The kill path, end to end, with the cwd shape production has.

    The child's cwd is its worktree — the exact input that made every
    real removal fail before, because deriving the repo root from it
    with `--show-toplevel` yields the worktree itself.
    """
    wt_path = wt.create_worktree(repo_root=repo, child_id="killme")
    p = _participant("killme", wt_path)

    _spawner().retire(p, delete_branch=True)

    assert not Path(wt_path).exists()
    verify = subprocess.run(
        ["git", "rev-parse", "--verify", "theater/killme"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert verify.returncode != 0
    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert "killme" not in listing.stdout


def test_retire_keeps_the_branch_of_a_child_that_exited(repo):
    """The reaper path: directory reclaimed, commits preserved."""
    wt_path = wt.create_worktree(repo_root=repo, child_id="exited")
    subprocess.run(["git", "commit", "--allow-empty", "-m", "child work"],
                   cwd=wt_path, check=True, capture_output=True)
    p = _participant("exited", wt_path)

    _spawner().retire(p, delete_branch=False)

    assert not Path(wt_path).exists()
    verify = subprocess.run(
        ["git", "rev-parse", "--verify", "theater/exited"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert verify.returncode == 0


def test_retire_ignores_a_participant_without_a_theater_branch(repo):
    """A child spawned without worktree=True shares the parent's checkout.

    Its cwd is the repo itself and its branch is whatever the user is on.
    Retiring it must touch nothing: the alternative is a daemon that
    deletes the branch a human is working on.
    """
    from theater.models import Participant, Tier

    p = Participant(id="plain", harness="vibe", tier=Tier.SPAWNED,
                    cwd=repo, branch="main")

    _spawner().retire(p, delete_branch=True)

    verify = subprocess.run(
        ["git", "rev-parse", "--verify", "main"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert verify.returncode == 0
    assert Path(repo, "README.md").exists()
