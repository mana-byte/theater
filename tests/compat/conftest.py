"""Fixtures for the harness-compat pipeline tests: a throwaway git repository to work in."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.harness_compat.contract import Check, Result, Verdict
from scripts.harness_compat.git import git as run_git
from scripts.harness_compat.policy import policy_relative

_POLICY = '{"min":"1.0.0","max":"1.2.0","excluded":[]}\n'


@pytest.fixture(autouse=True)
def _plain_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """No user, system or signing configuration reaches any git the tests (or the code) run."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


@pytest.fixture
def git() -> Callable[..., str]:
    """Run git in a repository as a throwaway committer."""

    def run(root: Path, *args: str) -> str:
        return run_git(root, "-c", "user.name=t", "-c", "user.email=t@example.com", *args)

    return run


@pytest.fixture
def repo(tmp_path: Path, git: Callable[..., str]) -> Path:
    """A committed repository holding the ``claude`` policy, one tracked file and an ignored one."""
    root = tmp_path / "repo"
    (root / policy_relative("claude").parent).mkdir(parents=True)
    (root / policy_relative("claude")).write_text(_POLICY, encoding="utf-8")
    (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


@pytest.fixture
def result() -> Callable[..., Result]:
    """A result for ``claude`` with one failing or passing check to match its verdict."""

    def make(version: str, verdict: Verdict = "green", *checks: Check) -> Result:
        if not checks:
            status = {"green": "pass", "red": "fail"}.get(verdict, "error")
            checks = (Check("conformance:launch", status, f"{verdict} detail"),)  # type: ignore[arg-type]
        return Result("claude", version, verdict, checks)

    return make
