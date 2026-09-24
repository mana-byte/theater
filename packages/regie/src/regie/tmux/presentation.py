"""Identity-safe tmux layout operations for the Régie UI."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass

from regie.contracts import LocalPresentationTarget, StageTarget, UnmanagedPane
from regie.tmux.clipboard import copy_to_system_clipboard
from regie.tmux.command import TmuxError, run
from regie.tmux.discovery import capture_process_snapshot, detect_harness
from regie.tmux.identity import PaneSnapshot, exact_match, pane_inventory, pane_snapshot
from regie.tmux.session import TmuxPresentationSession


@dataclass(frozen=True, slots=True)
class _Fence:
    server_identity: str
    provider_id: str
    terminal_incarnation: str
    occupant_id: str
    pane_pid: int | None


@dataclass(frozen=True, slots=True)
class _LocalFence:
    server_identity: str
    pane_pid: int


class TmuxPresentation:
    """Move panes only after verifying their durable provider evidence."""

    def __init__(self, *, expected_server_identity: str | None = None) -> None:
        self._session = TmuxPresentationSession(expected_server_identity=expected_server_identity)
        self._targets: dict[str, _Fence] = {}
        self._local_targets: dict[str, _LocalFence] = {}
        self._active_local_target: str | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        await self._session.open()

    async def close(self) -> None:
        await self._session.close()

    def can_stage(self, target: StageTarget) -> tuple[bool, str | None]:
        if isinstance(target, LocalPresentationTarget):
            fence = self._local_targets.get(target.terminal_id)
            if fence is None:
                return False, "pane is not in Régie's current unmanaged snapshot"
            if (
                self._session.server_identity is not None
                and fence.server_identity != self._session.server_identity
            ):
                return False, "pane belongs to another tmux server"
            return True, None
        if target.provider_kind != "tmux":
            return False, "terminal is not owned by the tmux provider"
        server_identity = target.occupant.get("tmux_server_identity")
        occupant_id = target.occupant.get("occupant_id")
        incarnation = target.occupant.get("terminal_incarnation")
        pane_pid = target.occupant.get("pane_pid")
        if target.occupant.get("provider_kind") != "tmux":
            return False, "terminal occupant evidence is not from the tmux provider"
        if not isinstance(server_identity, str) or not server_identity:
            return False, "terminal has no pinned tmux server identity"
        if (
            self._session.server_identity is not None
            and server_identity != self._session.server_identity
        ):
            return False, "terminal belongs to another tmux server"
        if not isinstance(occupant_id, str) or not occupant_id:
            return False, "terminal has no occupant identity"
        if incarnation != target.terminal_incarnation:
            return False, "terminal incarnation evidence is inconsistent"
        if pane_pid is not None and (type(pane_pid) is not int or pane_pid <= 0):
            return False, "terminal process evidence is invalid"
        self._targets[target.terminal_id] = _Fence(
            server_identity=server_identity,
            provider_id=target.provider_id,
            terminal_incarnation=target.terminal_incarnation,
            occupant_id=occupant_id,
            pane_pid=pane_pid,
        )
        return True, None

    async def target_window(self) -> str:
        """Return the local Régie window only under the pinned server identity."""
        return await self._session.require_window()

    async def _require_regie_window(self, expected_window: str | None = None) -> str:
        return await self._session.require_window(expected_window)

    async def terminal_exists(self, target: StageTarget) -> bool:
        allowed, _ = self.can_stage(target)
        if not allowed:
            return False
        try:
            await self._require_target(target)
        except TmuxError:
            return False
        return True

    async def stage_terminal(self, target: StageTarget, *, target_window: str) -> None:
        self._accept(target)
        async with self._lock:
            await self._require_target(target)
            await self._require_regie_window(target_window)
            join = ("join-pane", "-d", "-h", "-s", target.terminal_id, "-t", target_window)
            self._check_target(target, await pane_snapshot(target.terminal_id, *join))
            if isinstance(target, LocalPresentationTarget):
                self._active_local_target = target.terminal_id

    async def unstage_terminal(self, target: StageTarget) -> None:
        self._accept(target)
        async with self._lock:
            await self._require_target(target)
            name = (
                f"theater-local-{target.terminal_id.removeprefix('%')}"
                if isinstance(target, LocalPresentationTarget)
                else f"theater-{self._targets[target.terminal_id].occupant_id[:48]}"
            )
            try:
                self._check_target(
                    target,
                    await pane_snapshot(
                        target.terminal_id, "break-pane", "-d", "-s", target.terminal_id, "-n", name
                    ),
                )
            finally:
                if isinstance(target, LocalPresentationTarget):
                    self._active_local_target = None

    async def focus_terminal(self, target: StageTarget) -> None:
        self._accept(target)
        async with self._lock:
            await self._require_target(target)
            await run("select-pane", "-t", target.terminal_id)

    async def resize_regie(self, *, width: int) -> None:
        """Resize only the separately verified local Régie pane."""
        async with self._lock:
            await self._session.resize(width=width)

    async def resize_pane(
        self, pane_id: str, *, width: int | None = None, height: int | None = None
    ) -> None:
        if width is not None and (type(width) is not int or width <= 0):
            raise ValueError("pane width must be a positive integer")
        if height is not None and (type(height) is not int or height <= 0):
            raise ValueError("pane height must be a positive integer")
        async with self._lock:
            await self._require_exact(pane_id)
            if width is not None:
                await run("resize-pane", "-t", pane_id, "-x", str(width))
            if height is not None:
                await self._require_exact(pane_id)
                await run("resize-pane", "-t", pane_id, "-y", str(height))

    async def copy_text(self, text: str) -> None:
        """Copy to the tmux buffer and the system clipboard.

        `-w` also hands the text to the outer terminal's clipboard (OSC 52, so it
        works over SSH); a local clipboard tool covers terminals without OSC 52.
        """
        await self._require_regie_window()
        await run("set-buffer", "-w", "--", text)
        await copy_to_system_clipboard(text)

    async def unmanaged_panes(
        self, *, harness_commands: Mapping[str, tuple[str, ...]]
    ) -> tuple[UnmanagedPane, ...]:
        """Discover known harness panes without manufacturing a stageable identity."""
        await self._require_regie_window()
        snapshots = await pane_inventory()
        expected_server = self._session.server_identity
        current_pane = os.environ.get("TMUX_PANE")
        candidates = []
        for snapshot in snapshots:
            if expected_server is not None and snapshot.server_identity != expected_server:
                raise TmuxError("tmux server identity changed during unmanaged discovery")
            identity_values = (
                snapshot.provider_id,
                snapshot.terminal_incarnation,
                snapshot.occupant_id,
                snapshot.occupant_digest,
                snapshot.occupant_pane_pid,
                snapshot.launch_id,
                snapshot.launch_executable,
            )
            if snapshot.dead or snapshot.pane_id == current_pane or any(identity_values):
                continue
            candidates.append(snapshot)
        if not candidates or not harness_commands:
            self._replace_local_targets({})
            return ()
        processes = await asyncio.to_thread(capture_process_snapshot)
        rows: list[UnmanagedPane] = []
        local_targets: dict[str, _LocalFence] = {}
        for snapshot in candidates:
            harness = detect_harness(
                snapshot.executable,
                snapshot.pane_pid,
                processes,
                harness_commands,
            )
            if harness is None:
                continue
            local_targets[snapshot.pane_id] = _LocalFence(
                server_identity=snapshot.server_identity,
                pane_pid=snapshot.pane_pid,
            )
            rows.append(
                UnmanagedPane(
                    pane_id=snapshot.pane_id,
                    command=snapshot.executable or "?",
                    cwd=snapshot.cwd,
                    session=snapshot.session_name,
                    window_name=snapshot.window_name,
                    harness=harness,
                )
            )
        self._replace_local_targets(local_targets)
        return tuple(rows)

    def _replace_local_targets(self, targets: dict[str, _LocalFence]) -> None:
        active = self._active_local_target
        if active is not None and active in self._local_targets:
            targets.setdefault(active, self._local_targets[active])
        self._local_targets = targets

    def _accept(self, target: StageTarget) -> None:
        allowed, reason = self.can_stage(target)
        if not allowed:
            raise TmuxError(reason or "terminal cannot be staged")

    async def _require_target(self, target: StageTarget) -> _Fence | _LocalFence:
        return self._check_target(target, await pane_snapshot(target.terminal_id))

    def _check_target(
        self, target: StageTarget, snapshot: PaneSnapshot | None
    ) -> _Fence | _LocalFence:
        if isinstance(target, LocalPresentationTarget):
            return self._check_local(target.terminal_id, snapshot)
        return self._check_exact(target.terminal_id, snapshot)

    def _check_local(self, pane_id: str, snapshot: PaneSnapshot | None) -> _LocalFence:
        fence = self._local_targets.get(pane_id)
        if fence is None:
            raise TmuxError("pane is not in Régie's current unmanaged snapshot")
        if (
            snapshot is None
            or snapshot.dead
            or snapshot.server_identity != fence.server_identity
            or snapshot.pane_pid != fence.pane_pid
        ):
            if self._active_local_target == pane_id:
                self._active_local_target = None
            raise TmuxError("local pane no longer matches its discovered identity")
        return fence

    async def _require_exact(self, pane_id: str) -> _Fence:
        return self._check_exact(pane_id, await pane_snapshot(pane_id))

    def _check_exact(self, pane_id: str, snapshot: PaneSnapshot | None) -> _Fence:
        fence = self._targets.get(pane_id)
        if fence is None:
            raise TmuxError("pane has no previously supplied public terminal identity")
        if snapshot is None or not exact_match(
            snapshot,
            server_identity=fence.server_identity,
            provider_id=fence.provider_id,
            terminal_incarnation=fence.terminal_incarnation,
            occupant_id=fence.occupant_id,
            pane_pid=fence.pane_pid,
        ):
            raise TmuxError("pane no longer matches its public terminal identity")
        return fence


__all__ = ["TmuxPresentation"]
