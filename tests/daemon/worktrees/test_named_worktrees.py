"""Named worktrees: name validation, branch naming, creation and removal."""

from __future__ import annotations

import subprocess
from functools import partial
from pathlib import Path

import pytest

from tests.support.git_repos import committed_repo
from tests.support.tables import run_rows
from theater.daemon import worktree as wt
from theater.models import BadRequest


@pytest.fixture
def repo(tmp_path):
    """A real git repo with one commit."""
    return committed_repo(tmp_path)


def test_validate_name_rejects_and_accepts():
    """Names refuse blanks, separators, refs and oversize; normal names pass."""

    def rejects(name: str) -> None:
        with pytest.raises(BadRequest):
            wt.validate_name(name)

    def accepts(name: str) -> None:
        wt.validate_name(name)

    run_rows(
        [
            (f"reject:{label}", partial(rejects, name))
            for label, name in [
                ("empty", ""),
                ("blank", "   "),
                ("leading_dash", "-foo"),
                ("slash", "foo/bar"),
                ("dotdot", ".."),
                ("dot", "."),
                ("interior_dotdot", "foo/../bar"),
                ("main", "main"),
                ("HEAD", "HEAD"),
                ("overlong", "a" * 101),
            ]
        ]
        + [
            (f"accept:{name}", partial(accepts, name))
            for name in ["my-team", "feature_x", "bugfix-2", "shared.thing", "alpha"]
        ]
    )


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


@pytest.mark.parametrize(
    "name",
    ["trailing.", "double..dot", "ends.lock", "has..double"],
)
def test_validate_name_rejects_invalid_git_refs(repo, name):
    """Names that produce invalid git refs are rejected by check-ref-format."""
    with pytest.raises(BadRequest):
        wt.validate_name(name)
