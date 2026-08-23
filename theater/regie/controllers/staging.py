"""Move physical agent panes into and out of the régie window."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from theater.config import RegieSection

logger = logging.getLogger("theater.regie")


class PaneOperations(Protocol):
    """The tmux operations used by physical staging."""

    async def break_pane(self, pane_id: str, *, target_window: str | None = ...) -> None: ...

    async def join_pane(
        self, pane_id: str, *, target_window: str, horizontal: bool = ...
    ) -> None: ...

    async def resize_pane(
        self, pane_id: str, *, width: int | None = ..., height: int | None = ...
    ) -> None: ...


class StageOutcome(Enum):
    STAGED = "staged"
    UNSTAGED = "unstaged"
    NO_NODE = "no_node"
    NO_PANE = "no_pane"
    NO_WINDOW = "no_window"
    JOIN_FAILED = "join_failed"
    UNSTAGE_FAILED = "unstage_failed"
    FOOTER_ACTIVE = "footer_active"


@dataclass
class StageResult:
    outcome: StageOutcome
    staged_pane: str | None
    pane: str | None = None
    node_id: str | None = None
    error: str | None = None


@dataclass
class FocusResult:
    should_select: bool
    staged_pane: str | None
    pane: str | None = None
    stage_result: StageResult | None = None


class StageController:
    """Physical staging decisions with no Textual dependency."""

    def __init__(self, settings: RegieSection, ops: PaneOperations) -> None:
        self._settings = settings
        self._ops = ops

    async def stage(
        self,
        *,
        tree_lines: list,
        cursor: int,
        staged_pane: str | None,
        my_window: str | None,
        my_pane: str | None,
        footer_active: bool,
        selected_participant_fn,
    ) -> StageResult:
        """Stage or unstage the selected agent."""
        if footer_active:
            return StageResult(outcome=StageOutcome.FOOTER_ACTIVE, staged_pane=staged_pane)

        node = selected_participant_fn(tree_lines, cursor)
        if node is None:
            return StageResult(outcome=StageOutcome.NO_NODE, staged_pane=staged_pane)

        pane = node.get("tmux_pane")
        node_id = node.get("id")
        if not pane:
            return StageResult(
                outcome=StageOutcome.NO_PANE,
                staged_pane=staged_pane,
                node_id=node_id,
            )
        if not my_window:
            return StageResult(outcome=StageOutcome.NO_WINDOW, staged_pane=staged_pane)

        old_was_parked = False
        if staged_pane and staged_pane != pane:
            try:
                await self._ops.break_pane(staged_pane)
                old_was_parked = True
            except Exception as exc:
                return StageResult(
                    outcome=StageOutcome.UNSTAGE_FAILED,
                    staged_pane=staged_pane,
                    pane=pane,
                    node_id=node_id,
                    error=str(exc),
                )

        if staged_pane == pane:
            try:
                await self._ops.break_pane(pane)
                return StageResult(
                    outcome=StageOutcome.UNSTAGED,
                    staged_pane=None,
                    pane=pane,
                    node_id=node_id,
                )
            except Exception as exc:
                return StageResult(
                    outcome=StageOutcome.UNSTAGE_FAILED,
                    staged_pane=staged_pane,
                    pane=pane,
                    node_id=node_id,
                    error=str(exc),
                )

        try:
            await self._ops.join_pane(pane, target_window=my_window)
        except Exception as exc:
            return StageResult(
                outcome=StageOutcome.JOIN_FAILED,
                staged_pane=None if old_was_parked else staged_pane,
                pane=pane,
                node_id=node_id,
                error=str(exc),
            )
        if my_pane:
            try:
                await self._ops.resize_pane(my_pane, width=self._settings.sidebar_width)
            except Exception as exc:
                logger.debug("resize after stage failed: %s", exc)
        return StageResult(
            outcome=StageOutcome.STAGED,
            staged_pane=pane,
            pane=pane,
            node_id=node_id,
        )

    async def focus(
        self,
        *,
        tree_lines: list,
        cursor: int,
        staged_pane: str | None,
        my_window: str | None,
        my_pane: str | None,
        footer_active: bool,
        selected_participant_fn,
    ) -> FocusResult:
        """Focus an already staged pane, or stage it without focusing."""
        node = selected_participant_fn(tree_lines, cursor)
        pane = node.get("tmux_pane") if node else None

        if pane and pane == staged_pane:
            return FocusResult(should_select=True, staged_pane=staged_pane, pane=pane)

        result = await self.stage(
            tree_lines=tree_lines,
            cursor=cursor,
            staged_pane=staged_pane,
            my_window=my_window,
            my_pane=my_pane,
            footer_active=footer_active,
            selected_participant_fn=selected_participant_fn,
        )
        return FocusResult(
            should_select=False,
            staged_pane=result.staged_pane,
            pane=pane if result.staged_pane == pane else None,
            stage_result=result,
        )
