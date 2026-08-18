"""Harness detection from tmux pane state.

When `theater adopt` runs, the foreground process is `theater`/`uv`/`python3`,
not the harness session that is its ancestor in the process tree. This module
walks the process tree from the pane's shell pid to find the actual harness
binary.

The walk itself lives in `theater.proc`, because transcript correlation asks
the same operating system the same kind of question and a harness plugin must
not import from the daemon to do it.
"""

from __future__ import annotations

from theater import proc
from theater.harness import HARNESSES


def detect_harness(
    pane_command: str, pane_pid: int, snapshot: proc.ProcessSnapshot | None = None
) -> str:
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

    A caller checking several panes in one pass (the unmanaged sweep) may
    pass a `snapshot` captured once up front, so this walk costs no `ps` of
    its own. Omit it — the default — and a descendant walk here captures a
    fresh one, which is what single-pane callers like `adopt` and the
    delivery gate's stale-target check need.
    """
    name = match_binary(pane_command, HARNESSES)
    if name:
        return name
    if snapshot is not None:
        comms = descendant_comms(pane_pid, snapshot)
    else:
        comms = descendant_comms(pane_pid)
    for comm in comms:
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


def descendant_comms(root_pid: int, snapshot: proc.ProcessSnapshot | None = None) -> list[str]:
    """Process names of root_pid's descendants, breadth-first.

    Uses `ps` rather than /proc or psutil to stay dependency-free. The pane's
    shell spawned `vibe`, which spawned the bash tool running `theater adopt`
    — so `vibe` is in the tree even though it is not the foreground leaf.

    Walks a supplied `snapshot` if given, rather than capturing a fresh one —
    the caller already paid for the `ps` and this walk is nearly free.
    """
    pairs = snapshot.descendants(root_pid) if snapshot is not None else proc.descendants(root_pid)
    return [comm for _pid, comm in pairs]
