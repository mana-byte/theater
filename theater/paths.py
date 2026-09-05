"""Filesystem locations beneath ``THEATER_HOME``."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path, PureWindowsPath

_TMUX_PANE_ID = re.compile(r"^%([0-9]+)$")


def home() -> Path:
    return Path(os.environ.get("THEATER_HOME", Path.home() / ".theater"))


def config_path() -> Path:
    return home() / "config.toml"


def plugins_dir() -> Path:
    return home() / "plugins"


def skills_dir() -> Path:
    return home() / "skills"


def var_dir() -> Path:
    return home() / "var"


def state_dir() -> Path:
    return var_dir() / "state"


def run_dir() -> Path:
    return var_dir() / "run"


def logs_dir() -> Path:
    return var_dir() / "logs"


def participants_dir() -> Path:
    return var_dir() / "participants"


def keys_dir() -> Path:
    return state_dir() / "keys"


def plugin_states_dir() -> Path:
    return state_dir() / "plugins"


def plugin_state_dir(name: str) -> Path:
    return plugin_states_dir() / _component(name, "plugin name")


def db_path() -> Path:
    return state_dir() / "theater.db"


def socket_path() -> Path:
    return run_dir() / "daemon.sock"


def pidfile_path() -> Path:
    return run_dir() / "daemon.pid"


def daemon_logs_dir() -> Path:
    return logs_dir() / "daemon"


def daemon_stderr_logs_dir() -> Path:
    return daemon_logs_dir() / "stderr"


def regie_logs_dir() -> Path:
    return logs_dir() / "regie"


def plugin_logs_dir() -> Path:
    return logs_dir() / "plugins"


def plugin_log_dir(name: str) -> Path:
    return plugin_logs_dir() / _component(name, "plugin name")


def log_path() -> Path:
    return daemon_logs_dir() / "daemon.log"


def regie_log_path() -> Path:
    pane = os.environ.get("TMUX_PANE", "")
    match = _TMUX_PANE_ID.fullmatch(pane)
    identity = f"pane-{match.group(1)}" if match is not None else f"pid-{os.getpid()}"
    return regie_logs_dir() / f"{identity}.log"


def participant_dir(participant_id: str) -> Path:
    return participants_dir() / _component(participant_id, "participant id")


def participant_launch_dir(participant_id: str) -> Path:
    return participant_dir(participant_id) / "launch"


def participant_observation_dir(participant_id: str, harness: str) -> Path:
    return participant_dir(participant_id) / "observations" / _component(harness, "harness name")


def participant_plugin_dir(participant_id: str, plugin: str) -> Path:
    return participant_dir(participant_id) / "plugins" / _component(plugin, "plugin name")


def mcp_config_path(participant_id: str) -> Path:
    return participant_launch_dir(participant_id) / "mcp.json"


def marker_key_path(harness: str) -> Path:
    return keys_dir() / f"{_component(harness, 'harness name')}-domain-marker.key"


def ensure_home() -> Path:
    root = home()
    for directory in (
        root,
        plugins_dir(),
        skills_dir(),
        var_dir(),
        state_dir(),
        keys_dir(),
        plugin_states_dir(),
        run_dir(),
        logs_dir(),
        daemon_logs_dir(),
        daemon_stderr_logs_dir(),
        regie_logs_dir(),
        plugin_logs_dir(),
        participants_dir(),
    ):
        _mkdir(directory)
    return root


def ensure_private_file(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"Theater file is not a regular file: {path}")
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _mkdir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise OSError(f"Theater directory is not a real directory: {path}")
    path.chmod(0o700)


def _component(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{label} must be a non-empty path component")
    windows = PureWindowsPath(value)
    if Path(value).name != value or windows.name != value or value in {".", ".."}:
        raise ValueError(f"{label} must be one path component")
    return value
