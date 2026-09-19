"""Public-binding staging decisions for Régie's local tmux presentation."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from regie.contracts import PresentationOperations, PresentationTarget, RegieSettings
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
    target: PresentationTarget | None
    reason: str | None = None


class StageController:
    """Keep provider-backed participants visible without treating every route as tmux."""

    def __init__(self, settings: RegieSettings, ops: PresentationOperations) -> None:
        self._settings = settings
        self._ops = ops
        self._session = SessionController(ops)

    @property
    def staged_target(self) -> PresentationTarget | None:
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
        if self._session.target == target:
            result = await self._session.unstage()
            return StageResult(
                StageOutcome.UNSTAGED if result.reason is None else StageOutcome.FAILED,
                result.target,
                result.reason,
            )
        result = await self._session.stage(target)
        if result.staged:
            try:
                await self._ops.resize_regie(width=self._settings.sidebar_width)
            except Exception as exc:
                logger.debug("resize after stage failed: %s", exc)
        return StageResult(
            StageOutcome.STAGED if result.staged else StageOutcome.FAILED,
            result.target,
            result.reason,
        )

    async def focus(self) -> StageResult:
        result = await self._session.focus()
        return StageResult(
            StageOutcome.FOCUSED if result.staged else StageOutcome.UNAVAILABLE,
            result.target,
            result.reason,
        )

    async def unstage(self) -> StageResult:
        result = await self._session.unstage()
        return StageResult(
            StageOutcome.UNSTAGED if result.reason is None else StageOutcome.FAILED,
            result.target,
            result.reason,
        )

    async def close(self) -> None:
        await self._session.close()


__all__ = ["StageController", "StageOutcome", "StageResult"]
