"""Local presentation session state behind the frozen bridge operations boundary."""

from __future__ import annotations

from dataclasses import dataclass

from regie.contracts import PresentationOperations, PresentationTarget


@dataclass(frozen=True, slots=True)
class SessionResult:
    staged: bool
    target: PresentationTarget | None
    reason: str | None = None


class SessionController:
    """Stage, focus, and restore only already-existing bridge terminal identities."""

    def __init__(self, ops: PresentationOperations) -> None:
        self._ops = ops
        self._target: PresentationTarget | None = None

    @property
    def target(self) -> PresentationTarget | None:
        return self._target

    async def stage(self, target: PresentationTarget) -> SessionResult:
        if self._target == target:
            try:
                await self._ops.focus_terminal(target)
            except Exception as exc:
                return SessionResult(False, target, f"could not focus staged terminal: {exc}")
            return SessionResult(True, target)
        try:
            exists = await self._ops.terminal_exists(target)
        except Exception as exc:
            return SessionResult(False, self._target, f"could not verify terminal identity: {exc}")
        if not exists:
            return SessionResult(False, self._target, "terminal identity is no longer present")
        try:
            target_window = await self._ops.target_window()
        except Exception as exc:
            return SessionResult(
                False,
                self._target,
                f"could not locate Régie's target window: {exc}",
            )
        previous = self._target
        if previous is not None:
            try:
                await self._ops.unstage_terminal(previous)
            except Exception as exc:
                return SessionResult(False, previous, f"could not restore staged terminal: {exc}")
        try:
            await self._ops.stage_terminal(target, target_window=target_window)
        except Exception as exc:
            return SessionResult(False, None if previous is not None else previous, str(exc))
        self._target = target
        return SessionResult(True, target)

    async def unstage(self) -> SessionResult:
        target = self._target
        if target is None:
            return SessionResult(False, None, "no terminal is staged")
        try:
            await self._ops.unstage_terminal(target)
        except Exception as exc:
            return SessionResult(False, target, str(exc))
        self._target = None
        return SessionResult(False, None)

    async def focus(self) -> SessionResult:
        target = self._target
        if target is None:
            return SessionResult(False, None, "no terminal is staged")
        try:
            exists = await self._ops.terminal_exists(target)
        except Exception as exc:
            return SessionResult(False, target, f"could not verify terminal identity: {exc}")
        if not exists:
            self._target = None
            return SessionResult(False, None, "terminal identity changed while staged")
        try:
            await self._ops.focus_terminal(target)
        except Exception as exc:
            return SessionResult(False, target, f"could not focus terminal: {exc}")
        return SessionResult(True, target)

    async def close(self) -> None:
        """Restore local presentation; this never terminates a participant terminal."""
        await self.unstage()


__all__ = ["SessionController", "SessionResult"]
