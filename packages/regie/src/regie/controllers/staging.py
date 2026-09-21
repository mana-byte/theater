"""Public-binding staging decisions for Régie's local tmux presentation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from regie.contracts import (
    LocalPresentationTarget,
    PresentationOperations,
    PresentationTarget,
    RegieSettings,
    StageTarget,
)
from regie.controllers.session import SessionController
from regie.presentation import stageability
from theater.frontend import Participant, Provider

logger = logging.getLogger("regie")


class StageOutcome(StrEnum):
    STAGED = "staged"
    FOCUSED = "focused"
    UNSTAGED = "unstaged"
    UNSTAGEABLE = "unstageable"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StageResult:
    outcome: StageOutcome
    target: StageTarget | None
    reason: str | None = None


class StageController:
    """Keep provider-backed participants visible without treating every route as tmux."""

    def __init__(self, settings: RegieSettings, ops: PresentationOperations) -> None:
        self._settings = settings
        self._ops = ops
        self._session = SessionController(ops)
        self._lock = asyncio.Lock()

    @property
    def staged_target(self) -> StageTarget | None:
        return self._session.target

    async def open(self) -> None:
        await self._session.open()

    async def stage(
        self,
        participant: Participant,
        providers: Mapping[str, Provider],
    ) -> StageResult:
        eligibility = stageability(participant, providers, self._ops)
        target = eligibility.target
        if target is None:
            return StageResult(StageOutcome.UNAVAILABLE, None, eligibility.reason)
        if not eligibility.allowed:
            return StageResult(StageOutcome.UNSTAGEABLE, target, eligibility.reason)
        return await self._stage_target(target)

    async def stage_unmanaged(self, pane_id: str) -> StageResult:
        """Stage one pane from the local discovery snapshot without controlling it."""
        target = LocalPresentationTarget(pane_id)
        allowed, reason = self._ops.can_stage(target)
        if not allowed:
            return StageResult(StageOutcome.UNSTAGEABLE, target, reason)
        return await self._stage_target(target)

    async def _stage_target(self, target: StageTarget) -> StageResult:
        async with self._lock:
            if self._session.target == target:
                result = await self._session.unstage()
                if result.reason is not None:
                    return StageResult(
                        StageOutcome.FAILED,
                        result.target,
                        f"unstage failed: {result.reason}",
                    )
                return StageResult(
                    StageOutcome.UNSTAGED,
                    result.target,
                )
            result = await self._session.stage(target)
            if result.staged:
                try:
                    await self._ops.resize_regie(width=self._settings.sidebar_width)
                except Exception as exc:
                    logger.debug("resize after stage failed: %s", exc)
            if not result.staged:
                return StageResult(
                    StageOutcome.FAILED,
                    result.target,
                    f"stage failed: {result.reason or 'unknown error'}",
                )
            return StageResult(StageOutcome.STAGED, result.target)

    async def focus(self) -> StageResult:
        async with self._lock:
            result = await self._session.focus()
        return StageResult(
            StageOutcome.FOCUSED if result.staged else StageOutcome.UNAVAILABLE,
            result.target,
            result.reason,
        )

    async def unstage(self) -> StageResult:
        async with self._lock:
            return await self._unstage()

    async def unstage_participant(self, participant: Participant) -> StageResult | None:
        """Release only this participant's staged identity before requesting termination."""
        async with self._lock:
            target = self._session.target
            route = participant.terminal_route
            if not isinstance(target, PresentationTarget) or route is None:
                return None
            identity = route.identity
            if (
                target.provider_id != identity.provider_id
                or target.terminal_id != identity.terminal_id
                or target.terminal_incarnation != identity.terminal_incarnation
            ):
                return None
            return await self._unstage()

    async def _unstage(self) -> StageResult:
        result = await self._session.unstage()
        if result.reason is not None:
            return StageResult(
                StageOutcome.FAILED,
                result.target,
                f"unstage failed: {result.reason}",
            )
        return StageResult(
            StageOutcome.UNSTAGED,
            result.target,
        )

    async def reconcile(self) -> StageResult | None:
        """Clear presentation state when the exact staged terminal is gone."""
        async with self._lock:
            result = await self._session.reconcile()
        if result is None:
            return None
        return StageResult(StageOutcome.UNSTAGED, result.target)

    async def close(self) -> None:
        async with self._lock:
            await self._session.close()


__all__ = ["StageController", "StageOutcome", "StageResult"]
