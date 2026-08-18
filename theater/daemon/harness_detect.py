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

from enum import StrEnum

from theater import proc
from theater.harness import HARNESSES


class PaneHarnessVerdict(StrEnum):
    """The result of comparing a pane's foreground to a recorded harness.

    ``match``         — the pane positively identifies the same harness.
    ``undetermined``  — detection could not identify the harness (``"unknown"``)
                        and the foreground is NOT a shell.  Absence of evidence.
    ``conflict``      — detection positively identified a DIFFERENT harness.
    ``harness_gone``  — detection returned ``"unknown"`` AND the foreground is a
                        shell, meaning the CLI exited and left a prompt.
    """

    MATCH = "match"
    UNDETERMINED = "undetermined"
    CONFLICT = "conflict"
    HARNESS_GONE = "harness_gone"


def detect_harness(pane_command: str, pane_pid: int) -> str:
    """Map a pane to a canonical harness name, or 'unknown'.

    `pane_current_command` is the instantaneous foreground process — which,
    when `theater adopt` is the thing running, is `theater`/`uv`/`python3`,
    not the harness session that is its ancestor in the process tree.

    So we first check the foreground command (the common case when no adopt
    is in flight), then the pane root's own comm (the harness process IS the
    root for a Theater-spawned or wrapper-launched session, and
    ``proc.descendants`` excludes it), then walk the process tree from the
    pane's shell pid looking for any descendant whose name matches a known
    harness binary. The pane's shell spawned `vibe`, which spawned the bash
    tool running `theater adopt` — so `vibe` is in the tree even though it
    is not the foreground leaf.
    """
    name = match_binary(pane_command, HARNESSES)
    if name:
        return name
    # The pane root is the harness process for a Theater-spawned or
    # wrapper-launched session.  ``proc.descendants`` excludes it, so consult
    # it directly before the descendant walk.  This closes the false-negative
    # where the foreground is a tool subprocess (not the harness, not a
    # descendant) and detection would otherwise return "unknown".
    if pane_pid > 0:
        root_comm = proc.comm(pane_pid)
        if root_comm:
            name = match_binary(root_comm, HARNESSES)
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


def compare_detected_harness(
    recorded_harness: str,
    detected: str,
    pane_current_command: str,
) -> PaneHarnessVerdict:
    """Judge whether a pane still runs the recorded harness, given the
    ALREADY-DETECTED harness name.

    This is "job 2 — decide what it means" (AGENTS.md): the pure judgement
    with no I/O.  Callers that already have a detected name (e.g. from
    ``_get_pane_info``) call this directly, avoiding a redundant
    ``detect_harness`` call — which is a subprocess spawn on the fallback
    path and stalls the daemon event loop.

    Semantics:

    - ``recorded_harness == "unknown"`` → ``MATCH`` (nothing to compare).
    - detected equals recorded → ``MATCH``.
    - detected is a positively identified DIFFERENT harness → ``CONFLICT``.
    - detected is ``"unknown"`` AND foreground is a shell → ``HARNESS_GONE``.
    - detected is ``"unknown"`` and NOT a shell → ``UNDETERMINED``
      (absence of evidence, not evidence of a foreign harness).
    """
    if recorded_harness == "unknown":
        return PaneHarnessVerdict.MATCH
    if detected == recorded_harness:
        return PaneHarnessVerdict.MATCH
    if detected != "unknown":
        return PaneHarnessVerdict.CONFLICT
    if is_shell(pane_current_command):
        return PaneHarnessVerdict.HARNESS_GONE
    return PaneHarnessVerdict.UNDETERMINED


def compare_pane_harness(
    recorded_harness: str,
    pane_current_command: str,
    pane_pid: int,
) -> PaneHarnessVerdict:
    """One-call convenience: detect then judge.

    Calls ``detect_harness`` once and delegates to
    ``compare_detected_harness``.  Use this when you have a pane but no
    pre-detected name; use ``compare_detected_harness`` directly when the
    detection has already been done (e.g. ``pane_info["harness"]`` from
    ``_get_pane_info``) to avoid a redundant subprocess spawn.
    """
    detected = detect_harness(pane_current_command, pane_pid)
    return compare_detected_harness(recorded_harness, detected, pane_current_command)


def match_binary(command: str, harnesses) -> str | None:
    """Return the harness name if a command basename matches a harness binary.

    Two normalisations are applied to the basename before comparison:
    - A leading ``.`` is stripped (nixpkgs ``makeWrapper`` prefixes the
      wrapped binary with ``.`` and appends ``-wrapped``).
    - A trailing ``-wrapped`` is stripped.

    These are generic across harnesses — not special-cased per name — because
    the wrapper convention is shared. A plugin may also declare additional
    binary names via the ``binaries`` class attribute; the primary ``binary``
    is always included regardless of what ``binaries`` contains.
    """
    basename = command.rsplit("/", 1)[-1]
    normalised = _unwrap(basename)
    for harness in harnesses.values():
        names = {harness.binary} | harness.binaries
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
