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
    """Régie's own tree below one Theater home, laid out like Theater's.

    What people edit sits at the top; what Régie writes lives under ``var/``, split by lifetime
    into ``state`` (kept), ``run`` (this process's) and ``logs``. Theater's tree is never used.
    """

    theater_home: Path

    @property
    def root(self) -> Path:
        return self.theater_home / "regie"

    @property
    def config_path(self) -> Path:
        return self.root / "config.toml"

    @property
    def ambiences_dir(self) -> Path:
        """The user's own ambience plugins, one ``<name>/manifest.py`` each."""
        return self.root / "ambiences"

    @property
    def daemon_socket(self) -> Path:
        return self.theater_home / "var" / "run" / "daemon.sock"

    @property
    def var_dir(self) -> Path:
        return self.root / "var"

    @property
    def state_dir(self) -> Path:
        return self.var_dir / "state"

    @property
    def run_dir(self) -> Path:
        return self.var_dir / "run"

    @property
    def logs_dir(self) -> Path:
        return self.var_dir / "logs"

    @property
    def tree_layout_path(self) -> Path:
        return self.state_dir / "tree-layout.json"

    @property
    def bridge_state_dir(self) -> Path:
        return self.state_dir / "bridge"

    @property
    def bridge_process_lock(self) -> Path:
        return self.run_dir / "bridge-process.lock"

    @property
    def bridge_start_lock(self) -> Path:
        return self.run_dir / "bridge-start.lock"

    @property
    def bridge_pid_path(self) -> Path:
        return self.run_dir / "bridge.pid"

    @property
    def bridge_status_path(self) -> Path:
        return self.run_dir / "bridge.status.json"

    @property
    def bridge_log_path(self) -> Path:
        """The bridge worker's own rotating log."""
        return self.logs_dir / "bridge.log"

    @property
    def bridge_stderr_dir(self) -> Path:
        """Raw output of each bridge worker, one file per launch: where a crash lands."""
        return self.logs_dir / "bridge-stderr"

    @property
    def daemon_start_log_path(self) -> Path:
        """Output of a Theater daemon Régie had to start."""
        return self.logs_dir / "daemon-start.log"

    @property
    def ui_logs_dir(self) -> Path:
        return self.logs_dir / "ui"

    @property
    def ui_log_path(self) -> Path:
        pane = os.environ.get("TMUX_PANE", "")
        match = re.fullmatch(r"%([0-9]+)", pane)
        identity = f"pane-{match.group(1)}" if match is not None else f"pid-{os.getpid()}"
        return self.ui_logs_dir / f"{identity}.log"

    def ensure_private_runtime(self) -> None:
        for directory in (
            self.root,
            self.var_dir,
            self.state_dir,
            self.run_dir,
            self.logs_dir,
            self.bridge_state_dir,
            self.bridge_stderr_dir,
            self.ui_logs_dir,
        ):
            _private_dir(directory)


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
