"""Codex home and sessions-root resolution from environment evidence."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

CODEX_HOME_VAR = "CODEX_HOME"


def codex_home(env: Mapping[str, str] | None = None) -> Path | None:
    """CODEX_HOME exactly as Codex treats it: None when unset or empty."""
    source = os.environ if env is None else env
    raw = source.get(CODEX_HOME_VAR)
    if raw is None or raw == "":
        return None
    return Path(raw)


def sessions_root(*, cwd: str | None = None, env: Mapping[str, str] | None = None) -> Path:
    """The rollout sessions root for one participant's environment.

    Codex resolves a relative CODEX_HOME against the CLI process cwd, which for
    a Theater pane is the participant cwd.
    """
    return (_resolved_home(cwd=cwd, env=env)) / "sessions"


def home_for_launch(env: Mapping[str, str] | None = None) -> str:
    """The CODEX_HOME value a launch must pin so child and observer agree.

    Unset or empty pins "": Codex then uses its default home without requiring
    it to exist, and any tmux-inherited custom value is cleared. A relative
    value stays raw: the child resolves it against its own cwd.
    """
    home = codex_home(env)
    return str(home) if home is not None else ""


def _resolved_home(*, cwd: str | None = None, env: Mapping[str, str] | None = None) -> Path:
    home = codex_home(env)
    if home is None:
        return Path.home() / ".codex"
    if not home.is_absolute():
        home = (Path(cwd) if cwd else Path.cwd()) / home
    return home


__all__ = ["CODEX_HOME_VAR", "codex_home", "home_for_launch", "sessions_root"]
