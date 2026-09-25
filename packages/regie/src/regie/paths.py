"""Régie-owned paths below one Theater home without importing Theater config."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path


class RegiePathError(RuntimeError):
    """A private Régie path is not safe to use."""


@dataclass(frozen=True, slots=True)
class RegiePaths:
    """The standalone UI and bridge's private files for one Theater home."""

    theater_home: Path

    @property
    def root(self) -> Path:
        return self.theater_home / "regie"

    @property
    def config_path(self) -> Path:
        return self.root / "config.toml"

    @property
    def tree_layout_path(self) -> Path:
        return self.root / "tree-layout.json"

    @property
    def daemon_socket(self) -> Path:
        return self.theater_home / "var" / "run" / "daemon.sock"

    @property
    def bridge_state_dir(self) -> Path:
        return self.root / "bridge"

    @property
    def bridge_process_lock(self) -> Path:
        return self.root / "bridge-process.lock"

    @property
    def bridge_start_lock(self) -> Path:
        return self.root / "bridge-start.lock"

    @property
    def bridge_pid_path(self) -> Path:
        return self.root / "bridge.pid"

    @property
    def bridge_status_path(self) -> Path:
        return self.root / "bridge.status.json"

    @property
    def bridge_log_path(self) -> Path:
        return self.root / "bridge.log"

    @property
    def logs_dir(self) -> Path:
        return self.theater_home / "var" / "logs" / "regie"

    @property
    def ui_log_path(self) -> Path:
        pane = os.environ.get("TMUX_PANE", "")
        match = re.fullmatch(r"%([0-9]+)", pane)
        identity = f"pane-{match.group(1)}" if match is not None else f"pid-{os.getpid()}"
        return self.logs_dir / f"{identity}.log"

    def ensure_private_runtime(self) -> None:
        _private_dir(self.root)
        _private_dir(self.bridge_state_dir)
        _private_dir(self.logs_dir)


def paths_from_environment() -> RegiePaths:
    """Resolve only the ordinary Theater-home environment convention."""
    raw = os.environ.get("THEATER_HOME")
    home = Path(raw) if raw else Path.home() / ".theater"
    return RegiePaths(home)


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RegiePathError(f"Régie path is not a private directory: {path}")
    path.chmod(0o700)


__all__ = ["RegiePathError", "RegiePaths", "paths_from_environment"]
