"""Identity-safe tmux layout operations for the Régie UI."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from regie.contracts import PresentationTarget
from regie.tmux.command import TmuxError, run
from regie.tmux.identity import exact_match, pane_snapshot
from regie.tmux.session import TmuxPresentationSession


@dataclass(frozen=True, slots=True)
class _Fence:
    server_identity: str
    provider_id: str
    terminal_incarnation: str
    occupant_id: str
    pane_pid: int | None


class TmuxPresentation:
    """Move panes only after verifying their durable provider evidence."""

    def __init__(self, *, expected_server_identity: str | None = None) -> None:
        self._session = TmuxPresentationSession(expected_server_identity=expected_server_identity)
        self._targets: dict[str, _Fence] = {}
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        await self._session.open()

    async def close(self) -> None:
        await self._session.close()

    def can_stage(self, target: PresentationTarget) -> tuple[bool, str | None]:
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

    async def terminal_exists(self, target: PresentationTarget) -> bool:
        allowed, _ = self.can_stage(target)
        if not allowed:
            return False
        try:
            await self._require_exact(target.terminal_id)
        except TmuxError:
            return False
        return True

    async def stage_terminal(self, target: PresentationTarget, *, target_window: str) -> None:
        self._accept(target)
        async with self._lock:
            await self._require_exact(target.terminal_id)
            await self._require_regie_window(target_window)
            await run("join-pane", "-d", "-h", "-s", target.terminal_id, "-t", target_window)
            await self._require_exact(target.terminal_id)

    async def unstage_terminal(self, target: PresentationTarget) -> None:
        self._accept(target)
        async with self._lock:
            fence = await self._require_exact(target.terminal_id)
            name = f"theater-{fence.occupant_id[:48]}"
            await run("break-pane", "-d", "-s", target.terminal_id, "-n", name)
            await self._require_exact(target.terminal_id)

    async def focus_terminal(self, target: PresentationTarget) -> None:
        self._accept(target)
        async with self._lock:
            await self._require_exact(target.terminal_id)
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

    def _accept(self, target: PresentationTarget) -> None:
        allowed, reason = self.can_stage(target)
        if not allowed:
            raise TmuxError(reason or "terminal cannot be staged")

    async def _require_exact(self, pane_id: str) -> _Fence:
        fence = self._targets.get(pane_id)
        if fence is None:
            raise TmuxError("pane has no previously supplied public terminal identity")
        snapshot = await pane_snapshot(pane_id)
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
