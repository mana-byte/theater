"""Owned focus wake hooks and their cancellable tmux waiter."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re

from regie.tmux.command import TmuxError, run, sequence_argv
from regie.tmux.identity import ServerIdentity

_EVENTS = frozenset(
    (
        "client-focus-in",
        "client-focus-out",
        "pane-focus-in",
        "pane-focus-out",
        "client-attached",
        "client-detached",
        "client-session-changed",
        "client-active",
        "after-select-window",
        "after-select-pane",
        "after-new-window",
        "after-kill-pane",
        "after-split-window",
        "window-linked",
        "window-unlinked",
    )
)
_WINDOW_EVENTS = frozenset({"pane-focus-in", "pane-focus-out"})
_ENTRY = re.compile(r"^([\w-]+)\[(\d+)\] (.*)$")
_IDENTITY = "#{socket_path}\t#{pid}\t#{start_time}"


def _is_global(scope: tuple[str, ...]) -> bool:
    return "-g" in scope


class FocusHooks:
    def __init__(self, server_identity: str) -> None:
        self.identity = ServerIdentity.parse(server_identity)
        self.channel = "regie-presence-" + hashlib.sha256(server_identity.encode()).hexdigest()[:16]
        self.command = f"wait-for -S {self.channel}"
        # Session and window scopes already carrying our hook; tmux ids never recur.
        self._armed_targets: set[tuple[str, ...]] = set()

    async def _run(self, *args: str, **kwargs) -> str:
        return await run("-S", self.identity.socket_path, *args, **kwargs)

    async def _verify(self) -> None:
        observed = await self._run("display-message", "-p", _IDENTITY)
        if observed.split("\t") != [
            self.identity.socket_path,
            self.identity.pid,
            self.identity.started_at,
        ]:
            raise TmuxError("the focus hook server identity changed")

    async def _scopes(self) -> list[tuple[str, ...]]:
        inventory = await self._run(
            *sequence_argv(
                (
                    ("list-sessions", "-F", "session\t#{session_id}"),
                    ("list-windows", "-a", "-F", "window\t#{window_id}"),
                )
            )
        )
        sessions: list[str] = []
        windows: list[str] = []
        for row in inventory.splitlines():
            kind, separator, target = row.partition("\t")
            if not separator or kind not in {"session", "window"} or not target:
                raise TmuxError("tmux returned invalid focus hook scopes")
            (sessions if kind == "session" else windows).append(target)
        return [
            ("-g",),
            ("-g", "-w"),
            *(("-t", session) for session in sessions),
            *(("-w", "-t", window) for window in sorted(set(windows))),
        ]

    async def _entries(self, scope: tuple[str, ...]) -> dict[str, dict[int, str]]:
        # A session or window may close between listing and reading it; that is
        # an empty scope, not a failure. Global scopes must always answer.
        output = await self._run("show-hooks", *scope, check=_is_global(scope))
        entries: dict[str, dict[int, str]] = {}
        for line in output.splitlines():
            match = _ENTRY.fullmatch(line)
            if match and match[1] in _EVENTS:
                entries.setdefault(match[1], {})[int(match[2])] = match[3]
        return entries

    async def arm(self) -> bool:
        """Preserve user hook slots; return whether focus reporting was already enabled."""
        await self._verify()
        enabled = await self._run("show-options", "-g", "-v", "focus-events")
        if enabled != "on":
            await self._run("set-option", "-g", "focus-events", "on")
        if await self._run("show-options", "-g", "-v", "focus-events") != "on":
            raise TmuxError("tmux focus reporting could not be enabled; presence remains protected")
        scopes = await self._scopes()
        for scope in scopes:
            if scope in self._armed_targets:
                continue
            entries = await self._entries(scope)
            events = _WINDOW_EVENTS if "-w" in scope else _EVENTS - _WINDOW_EVENTS
            commands: list[tuple[str, ...]] = []
            for event in sorted(events if _is_global(scope) else entries):
                slots = entries.get(event, {})
                if self.command not in slots.values():
                    index = max(slots, default=-1) + 1
                    commands.append(("set-hook", *scope, f"{event}[{index}]", self.command))
            if commands:
                await self._run(*sequence_argv(commands), check=_is_global(scope))
            if not _is_global(scope):
                self._armed_targets.add(scope)
        self._armed_targets.intersection_update(scopes)
        await self._verify()
        return enabled == "on"

    async def close(self) -> None:
        """Remove only our hook bodies; leave focus-events enabled for attached clients."""
        await self._verify()
        for scope in await self._scopes():
            for event, slots in (await self._entries(scope)).items():
                for index, body in slots.items():
                    if body == self.command:
                        await self._run(
                            "set-hook", *scope, "-u", f"{event}[{index}]", check=_is_global(scope)
                        )
        self._armed_targets.clear()

    async def wait(self) -> None:
        process = await asyncio.create_subprocess_exec(
            "tmux",
            "-S",
            self.identity.socket_path,
            "wait-for",
            self.channel,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            code = await process.wait()
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise
        if code:
            raise TmuxError("the tmux focus wake connection was lost")
