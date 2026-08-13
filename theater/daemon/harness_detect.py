"""Harness detection from tmux pane state.

When `theater adopt` runs, the foreground process is `theater`/`uv`/`python3`,
not the harness session that is its ancestor in the process tree. This module
walks the process tree from the pane's shell pid to find the actual harness
binary.
"""

from __future__ import annotations

import subprocess

from theater.harness import HARNESSES


def detect_harness(pane_command: str, pane_pid: int) -> str:
    """Map a pane to a canonical harness name, or 'unknown'.

    `pane_current_command` is the instantaneous foreground process — which,
    when `theater adopt` is the thing running, is `theater`/`uv`/`python3`,
    not the harness session that is its ancestor in the process tree.

    So we first check the foreground command (the common case when no adopt
    is in flight), then walk the process tree from the pane's shell pid
    looking for any descendant whose name matches a known harness binary.
    The pane's shell spawned `vibe`, which spawned the bash tool running
    `theater adopt` — so `vibe` is in the tree even though it is not the
    foreground leaf.
    """
    name = match_binary(pane_command, HARNESSES)
    if name:
        return name
    for comm in descendant_comms(pane_pid):
        name = match_binary(comm, HARNESSES)
        if name:
            return name
    return "unknown"


#: Interactive shells a pane falls back to when the program running in it
#: exits. Not exhaustive and does not need to be: a name missing from this set
#: only costs a refusal we could have made, never a wrong delivery.
SHELLS = frozenset(
    {"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "nu", "xonsh", "elvish"}
)


def is_shell(command: str) -> bool:
    """Whether a pane's foreground command is an interactive shell.

    Used as the second half of the "the CLI died" test. On its own it proves
    nothing — an agent running its bash tool also puts a shell in the
    foreground — so it is only ever read together with the absence of any
    harness in the process tree.
    """
    return command.rsplit("/", 1)[-1] in SHELLS


def match_binary(command: str, harnesses) -> str | None:
    """Return the harness name if a command basename matches a harness binary."""
    basename = command.rsplit("/", 1)[-1]
    for harness in harnesses.values():
        if harness.binary in (basename, command):
            return harness.name
    return None


def descendant_comms(root_pid: int) -> list[str]:
    """Process names of root_pid and all its descendants, breadth-first.

    Uses `ps` rather than /proc or psutil to stay dependency-free. The pane's
    shell spawned `vibe`, which spawned the bash tool running `theater adopt`
    — so `vibe` is in the tree even though it is not the foreground leaf.
    """
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid,ppid,comm"],
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    # Build ppid -> [(pid, comm)] map.
    pid_children: dict[int, list[tuple[int, str]]] = {}
    for line in out.strip().splitlines()[1:]:  # skip header
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        comm = parts[2]
        pid_children.setdefault(ppid, []).append((pid, comm))
    # BFS from root_pid, collecting comm names.
    result: list[str] = []
    queue = [root_pid]
    while queue:
        pid = queue.pop(0)
        for child_pid, comm in pid_children.get(pid, []):
            result.append(comm)
            queue.append(child_pid)
    return result
