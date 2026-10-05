"""Fail-closed detection of custom agent profiles shadowing the approval profiles.

Verified against real wheels (2.24.1 and 2.25.8): a same-name custom file in a
project or user agents dir replaces the builtin profile, and profile overrides
merge ABOVE the environment layer — so ``VIBE_BYPASS_TOOL_PERMISSIONS=false``
cannot neutralise a profile-level ``bypass_tool_permissions = true``.
"""

from __future__ import annotations

import os
from pathlib import Path

from .constants import VIBE_HOME_ENV

_PROJECT_AGENTS_SUBDIR = Path(".vibe") / "agents"


def vibe_home() -> Path:
    """Where vibe resolves its home: ``$VIBE_HOME`` wins, else ``~/.vibe``."""
    home = os.environ.get(VIBE_HOME_ENV)
    return Path(home).expanduser() if home else Path.home() / ".vibe"


def shadowed_approval_profile(agent: str, cwd: Path | None) -> Path | None:
    """The custom profile file vibe would load over the builtin ``agent``, if any.

    Conservative: trust state and config ``agent_paths`` are not replicated, so a
    hit is refused even when vibe's own trust gate would have skipped the file.
    """
    candidates = [Path(cwd) / _PROJECT_AGENTS_SUBDIR / f"{agent}.toml"] if cwd is not None else []
    candidates.append(vibe_home() / "agents" / f"{agent}.toml")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


__all__ = ["shadowed_approval_profile", "vibe_home"]
