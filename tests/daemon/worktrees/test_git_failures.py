"""Git failures during worktree removal: timeouts, missing binary and indeterminate verification."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.support.git_repos import committed_repo
from theater.daemon import worktree as wt


@pytest.fixture
def repo(tmp_path):
    """A real git repo with one commit."""
    return committed_repo(tmp_path)


def test_git_returns_failed_result_on_timeout(monkeypatch):
    """_git() synthesizes a failed CompletedProcess (rc 124) so a stuck git cannot hang callers."""

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
    # Compatibility: old private name still present on the façade.
    assert wt._INDETERMINATE_RCS == wt.INDETERMINATE_RCS


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


@pytest.mark.parametrize(
    ("returncode", "stderr", "indeterminate_error"),
    [
        pytest.param(124, "git timed out", True, id="timeout"),
        pytest.param(127, "git: not found", False, id="missing_git"),
        pytest.param(128, "fatal: not a valid ref", False, id="git_fatal_128"),
    ],
)
def test_remove_worktree_indeterminate_verify_does_not_report_branch_removed(
    monkeypatch, repo, returncode, stderr, indeterminate_error
):
    """A failed ``branch -D`` whose verify ``rev-parse`` is indeterminate must not mean 'gone'."""
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
                args=argv, returncode=returncode, stdout="", stderr=stderr
            )
        return real_run(argv, **kwargs)

    monkeypatch.setattr(wt_mod.subprocess, "run", fake_run)

    result = wt.remove_worktree(repo_root=repo, child_id="indeterminate-test", delete_branch=True)

    assert result.branch_removed is False, f"rc={returncode} must NOT be treated as 'branch gone'"
    assert result.ok is False
    if indeterminate_error:
        assert any("indeterminate" in e for e in result.errors)


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
