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
    """Return the harness name if a command basename matches a harness binary.

    Two normalisations are applied to the basename before comparison:
    - A leading ``.`` is stripped (nixpkgs ``makeWrapper`` prefixes the
      wrapped binary with ``.`` and appends ``-wrapped``).
    - A trailing ``-wrapped`` is stripped.

    These are generic across harnesses — not special-cased per name — because
    the wrapper convention is shared. A plugin may also declare additional
    binary names via the ``binaries`` class attribute.
    """
    basename = command.rsplit("/", 1)[-1]
    normalised = _unwrap(basename)
    for harness in harnesses.values():
        names = harness.binaries or {harness.binary}
        if basename in names or normalised in names or command in names:
            return harness.name
    return None


def _unwrap(basename: str) -> str:
    """Strip nixpkgs makeWrapper affixes from a binary basename.

    ``.claude-wrapped`` → ``claude``. Kept generic: no per-harness
    special-casing, only the leading ``.`` and trailing ``-wrapped``
    that ``makeWrapper`` adds.
    """
    name = basename
    if name.startswith("."):
        name = name[1:]
    if name.endswith("-wrapped"):
        name = name[: -len("-wrapped")]
    return name


def descendant_comms(root_pid: int) -> list[str]:
    """Process names of root_pid's descendants, breadth-first.

    Uses `ps` rather than /proc or psutil to stay dependency-free. The pane's
    shell spawned `vibe`, which spawned the bash tool running `theater adopt`
    — so `vibe` is in the tree even though it is not the foreground leaf.
    """
    return [comm for _pid, comm in proc.descendants(root_pid)]
