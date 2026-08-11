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


def mcp_config_dir() -> Path:
    """Per-participant MCP server configs, written at spawn time."""
    return home() / "mcp"


def ensure_home() -> Path:
    root = home()
    root.mkdir(parents=True, exist_ok=True)
    mcp_config_dir().mkdir(parents=True, exist_ok=True)
    return root
