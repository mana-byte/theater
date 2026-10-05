"""Claude configuration-root resolution.

$CLAUDE_CONFIG_DIR selects a nondefault profile; observation, receipt
validation, and resume must follow it or its transcripts are rejected.
"""

from __future__ import annotations

import os
from pathlib import Path


def claude_config_root() -> Path:
    """Claude's config root: $CLAUDE_CONFIG_DIR when set and non-empty, else ~/.claude."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        # Absolute so the daemon's reads and the child's writes name one folder
        # even when the child's cwd (a worktree) differs from the daemon's.
        return Path(configured).expanduser().absolute()
    return Path.home() / ".claude"


def claude_config_env() -> dict[str, str]:
    """Launch env pinning the child to the observer's root; empty when unset.

    The default is deliberately never pinned: pinning could change which
    credentials the child resolves for users who never set the variable.
    """
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        return {"CLAUDE_CONFIG_DIR": str(claude_config_root())}
    return {}


def claude_projects_root() -> Path:
    """Claude's transcript projects root under the resolved config root."""
    return claude_config_root() / "projects"
