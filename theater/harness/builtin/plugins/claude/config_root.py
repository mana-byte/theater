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
        return Path(configured).expanduser()
    return Path.home() / ".claude"


def claude_projects_root() -> Path:
    """Claude's transcript projects root under the resolved config root."""
    return claude_config_root() / "projects"
