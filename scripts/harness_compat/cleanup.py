"""Stop whatever a qualification left running from its sandbox: tmux servers and stray processes."""

from __future__ import annotations

import contextlib
import os
import re
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path

from .process import ancestry, descendants, kill_all, process_table

_SKIPPED_DIRS = frozenset({"node_modules", ".git", "uv-cache", "uv-tools", "npm"})


def _sockets(root: Path) -> Iterator[Path]:
    for directory, names, files in os.walk(root):
        names[:] = [name for name in names if name not in _SKIPPED_DIRS]
        for name in files:
            path = Path(directory, name)
            with contextlib.suppress(OSError):
                if stat.S_ISSOCK(path.lstat().st_mode):
                    yield path


def stop_tmux_server(socket: Path) -> None:
    """Ask the tmux server behind ``socket`` to exit; a socket nobody serves is ignored."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["tmux", "-S", str(socket), "kill-server"],
            capture_output=True,
            timeout=5,
            check=False,
        )


def stop_sandbox_processes(root: Path, *sockets: Path) -> None:
    """Kill tmux servers on ``sockets`` or any socket under ``root``, then processes run from it."""
    for socket in dict.fromkeys([*(s for s in sockets if s.exists()), *_sockets(root)]):
        stop_tmux_server(socket)
    if len(root.parts) < 3:  # never match processes by a path as broad as /tmp
        return
    table = process_table()
    needle = re.compile(re.escape(str(root)) + r"(?=/|\s|$)")
    protected = ancestry(os.getpid(), table)
    matched = {pid for pid, (_, command) in table.items() if needle.search(command)}
    victims = set(matched)
    for pid in matched:
        victims |= descendants(pid, table)
    kill_all(victims - protected)
