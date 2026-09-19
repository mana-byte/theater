"""Local process facts for finding unadopted harness panes."""

from __future__ import annotations

import subprocess
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field

_PROCESS_TIMEOUT_SECONDS = 5
_OBSERVED_COMMAND_MAX = 15


@dataclass(frozen=True, slots=True)
class ProcessSnapshot:
    """One best-effort process table captured for an unmanaged-pane sweep."""

    children: Mapping[int, tuple[tuple[int, str], ...]] = field(default_factory=dict)
    commands: Mapping[int, str] = field(default_factory=dict)

    def below(self, root_pid: int) -> tuple[str, ...]:
        found: list[str] = []
        queue = deque([root_pid])
        seen = {root_pid}
        while queue:
            parent = queue.popleft()
            for pid, command in self.children.get(parent, ()):
                if pid in seen:
                    continue
                seen.add(pid)
                found.append(command)
                queue.append(pid)
        return tuple(found)


def capture_process_snapshot() -> ProcessSnapshot:
    """Read the process table once; unavailable process facts are not evidence."""
    try:
        completed = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,comm="],
            check=False,
            capture_output=True,
            text=True,
            timeout=_PROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return ProcessSnapshot()
    if completed.returncode != 0:
        return ProcessSnapshot()
    children: dict[int, list[tuple[int, str]]] = {}
    commands: dict[int, str] = {}
    for line in completed.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        try:
            pid, parent = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        command = parts[2]
        commands[pid] = command
        children.setdefault(parent, []).append((pid, command))
    return ProcessSnapshot(
        children={key: tuple(value) for key, value in children.items()},
        commands=commands,
    )


def detect_harness(
    foreground: str,
    pane_pid: int,
    processes: ProcessSnapshot,
    harness_commands: Mapping[str, tuple[str, ...]],
) -> str | None:
    """Match foreground, pane root, then descendants against public catalog commands."""
    index = _command_index(harness_commands)
    commands = (foreground, processes.commands.get(pane_pid, ""), *processes.below(pane_pid))
    for command in commands:
        matches = {_match for key in _observed_keys(command) if (_match := index.get(key))}
        if len(matches) == 1:
            return matches.pop()
    return None


def _command_index(harness_commands: Mapping[str, tuple[str, ...]]) -> dict[str, str | None]:
    claims: dict[str, set[str]] = {}
    for harness, commands in harness_commands.items():
        for command in (harness, *commands):
            base = _normalize(command)
            if not base:
                continue
            for spelling in {base, f".{base}-wrapped", f"{base}-wrapped"}:
                keys = {spelling, _normalize(spelling)}
                if len(spelling) > _OBSERVED_COMMAND_MAX:
                    keys.add(spelling[:_OBSERVED_COMMAND_MAX])
                for key in keys:
                    if key:
                        claims.setdefault(key, set()).add(harness)
    return {
        command: next(iter(owners)) if len(owners) == 1 else None
        for command, owners in claims.items()
    }


def _observed_keys(command: str) -> frozenset[str]:
    base = command.rsplit("/", 1)[-1]
    return frozenset({base, _normalize(base)}) - {""}


def _normalize(command: str) -> str:
    name = command.rsplit("/", 1)[-1]
    if name.startswith("."):
        name = name[1:]
    if name.endswith("-wrapped"):
        name = name[: -len("-wrapped")]
    return name


__all__ = ["ProcessSnapshot", "capture_process_snapshot", "detect_harness"]
