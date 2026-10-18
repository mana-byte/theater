"""Fixtures for the Codex harness tests: a fake pane process tree for correlation."""

from __future__ import annotations

import pytest

from tests.support.codex_process_correlation import SESSION_A, SESSION_B
from tests.support.codex_rollouts import write_rollout


@pytest.fixture
def codex_tree(tmp_path):
    """A codex session root with two rollouts sharing one working directory."""
    root = tmp_path / ".codex" / "sessions"
    root.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    older = write_rollout(root, SESSION_A, project, at="01-19-02", text="agent A")
    newer = write_rollout(root, SESSION_B, project, at="01-19-46", text="agent B")
    return {"root": root, "project": project, "a": older, "b": newer}
