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
    """Resolve relative CODEX_HOME against the participant's CLI working directory."""
    return _resolved_home(cwd=cwd, env=env) / "sessions"


def home_for_launch(env: Mapping[str, str] | None = None) -> str:
    """Clear inherited custom homes for native defaults; preserve explicit relative values."""
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
