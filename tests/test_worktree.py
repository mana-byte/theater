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

    wt.remove_worktree(repo_root=repo, child_id="child4")

    assert not Path(wt_path).exists()
    # The branch is gone
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "theater/child4"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0


def test_remove_worktree_with_uncommitted_changes(repo):
    """Removing a worktree with uncommitted changes uses --force."""
    wt_path = wt.create_worktree(repo_root=repo, child_id="child5")
    (Path(wt_path) / "uncommitted.txt").write_text("dirty\n")
    # Should not raise
    wt.remove_worktree(repo_root=repo, child_id="child5")
    assert not Path(wt_path).exists()
