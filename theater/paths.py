"""Filesystem locations. Everything is under THEATER_HOME so tests can relocate it."""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    """Root for all Theater state. Override with $THEATER_HOME."""
    return Path(os.environ.get("THEATER_HOME", Path.home() / ".theater"))


def db_path() -> Path:
    return home() / "theater.db"


def socket_path() -> Path:
    return home() / "daemon.sock"


def log_path() -> Path:
    return home() / "daemon.log"


def pidfile_path() -> Path:
    """The daemon's lockfile. Held with flock; see theater/daemon/lock.py.

    The pid inside is for a human reading the file. Whether a daemon is running
    is answered by whether the lock is held, never by this number.
    """
    return home() / "daemon.pid"


def mcp_config_dir() -> Path:
    """Per-participant MCP server configs, written at spawn time."""
    return home() / "mcp"


def mcp_config_path(participant_id: str) -> Path:
    """The generated MCP config for one participant."""
    return mcp_config_dir() / f"{participant_id}.json"


def observation_dir(harness: str, participant_id: str) -> Path:
    """Process-correlation state owned by one launched harness instance."""
    return home() / "observations" / harness / participant_id


def config_path() -> Path:
    """User settings. Read by Theater, never written by it — see config.py."""
    return home() / "config.toml"


def harnesses_dir() -> Path:
    """Python harness plugins, imported at start-up.

    See docs/harness-plugins.md. Created empty by `ensure_home` rather than on
    first use: the directory existing is how a user finds out the extension
    point exists at all.
    """
    return home() / "harnesses"


def ensure_home() -> Path:
    root = home()
    root.mkdir(parents=True, exist_ok=True)
    mcp_config_dir().mkdir(parents=True, exist_ok=True)
    (root / "observations").mkdir(parents=True, exist_ok=True)
    harnesses_dir().mkdir(parents=True, exist_ok=True)
    return root
