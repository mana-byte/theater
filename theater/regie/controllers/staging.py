"""Staging controller: move agent panes into and out of the régie window.

``StageController`` owns the staging decision logic and tmux pane mechanics
that ``action_stage`` and ``action_focus_stage`` used to inline. It receives
an explicit ``PaneOperations`` collaborator rather than ``RegieApp`` or an
untyped service locator, and it resolves tmux functions at call time so tests
that monkeypatch ``app_mod.panes`` after app construction still work.

The controller never touches Textual widgets, reactives, or notifications.
It returns explicit typed outcomes and the app performs all side effects
(notifications, reactive assignment, tree rendering) in the same order as
before, preserving the exact observable failure semantics:

- switching panes attempts break of old before join of new;
- failure breaking old is debug-only and join continues;
- if old break succeeds but new join fails, ``staged_pane`` stays the old value;
- resize failure is debug-only;
- focusing after a failed switch never selects the stale old pane.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from theater.config import RegieSection

logger = logging.getLogger("theater.regie")


@runtime_checkable
class PaneOperations(Protocol):
    """The tmux pane operations the staging controller needs, resolved at call time.

    This mirrors :mod:`theater.tmux.panes` so the app can pass the module
    object directly and tests that monkeypatch ``app_mod.panes`` after
    construction still see the patched functions.
    """

    async def break_pane(self, pane_id: str, *, target_window: str | None = ...) -> None: ...

    async def join_pane(
        self, pane_id: str, *, target_window: str, horizontal: bool = ...
    ) -> None: ...

    async def resize_pane(
        self, pane_id: str, *, width: int | None = ..., height: int | None = ...
    ) -> None: ...

    async def select_pane(self, pane_id: str) -> None: ...


class StageOutcome(Enum):
    """What ``stage`` needs the app to do after the controller runs."""

    STAGED = "staged"
    UNSTAGED = "unstaged"
    ALREADY_STAGED = "already_staged"
    NO_NODE = "no_node"
    NO_PANE = "no_pane"
    NO_WINDOW = "no_window"
    JOIN_FAILED = "join_failed"
    UNSTAGE_FAILED = "unstage_failed"
    FOOTER_ACTIVE = "footer_active"


@dataclass
class StageResult:
    """Outcome of a staging operation and the new staged_pane value.

    ``outcome`` tells the app what notification/reaction to perform.
    ``staged_pane`` is the value the app's reactive should end up with;
    the controller does not assign it directly.
    """

    outcome: StageOutcome
    staged_pane: str | None
    pane: str | None = None
    node_id: str | None = None
    error: str | None = None


@dataclass
class FocusResult:
    """Outcome of a focus operation and the new staged_pane value.

    ``should_select`` is True only when the app should call ``select_pane``.
    When the staging failed, it is False so a stale old pane is never focused.
    ``stage_result`` carries the underlying staging outcome so the app can
    issue the same notifications ``action_stage`` would have produced.
    """

    should_select: bool
    staged_pane: str | None
    pane: str | None = None
    stage_result: StageResult | None = None


class StageController:
    """Staging decision logic and tmux pane mechanics, no app reference.

    Constructed with the loaded ``RegieSection`` (for ``sidebar_width``) and
    a ``PaneOperations`` collaborator (the ``theater.tmux.panes`` module or a
    test double). The app passes ``tree_lines``, ``cursor``, ``staged_pane``,
    ``my_window``, ``my_pane``, and ``footer_active`` per call so the
    controller never holds mutable display state.
    """

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
        """Stage or unstage the selected agent.

        Resolves the selected node, handles the toggle (unstage) case, breaks
        the old occupant before joining the new one, and resizes the régie
        pane after a successful join. Break/resize failures are debug-only;
        a join failure leaves ``staged_pane`` at its old value.
        """
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

        # Break the old occupant before joining the new one.
        if staged_pane and staged_pane != pane:
            try:
                await self._ops.break_pane(staged_pane)
            except Exception as exc:
                logger.debug("unstage failed: %s", exc)

        # Toggle: unstaging the current occupant.
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

        # Join the new pane and resize the régie pane.
        try:
            await self._ops.join_pane(pane, target_window=my_window)
        except Exception as exc:
            return StageResult(
                outcome=StageOutcome.JOIN_FAILED,
                staged_pane=staged_pane,
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
        """Stage the selected agent if needed, then focus it.

        Returns ``should_select=True`` only when the pane that ended up
        staged matches the selected node's pane. When staging fails,
        ``should_select`` is False so a stale old pane is never focused.
        """
        node = selected_participant_fn(tree_lines, cursor)
        pane = node.get("tmux_pane") if node else None

        # Fast path: the selected pane is already the staged pane.
        if pane and pane == staged_pane:
            if not staged_pane:
                return FocusResult(should_select=False, staged_pane=staged_pane)
            return FocusResult(should_select=True, staged_pane=staged_pane, pane=pane)

        # Need to stage (or re-stage) the selected agent.
        result = await self.stage(
            tree_lines=tree_lines,
            cursor=cursor,
            staged_pane=staged_pane,
            my_window=my_window,
            my_pane=my_pane,
            footer_active=footer_active,
            selected_participant_fn=selected_participant_fn,
        )
        # Staging failed or there was nothing to stage: don't focus.
        if result.staged_pane != pane:
            return FocusResult(
                should_select=False, staged_pane=result.staged_pane, stage_result=result
            )
        if not result.staged_pane:
            return FocusResult(should_select=False, staged_pane=None, stage_result=result)
        return FocusResult(
            should_select=True, staged_pane=result.staged_pane, pane=pane, stage_result=result
        )
