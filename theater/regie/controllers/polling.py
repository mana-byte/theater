"""Daemon polling state and decisions for tree, bus, and animation.

``PollingController`` owns the two bus cursors plus animation priming state.
The app passes its connected ``DaemonClient`` per call so the controller never
holds a reference to it. It returns explicit typed results and decisions so
the app can perform all Textual/RichLog rendering and reactive tree assignment
without the controller touching any widget.

The controller preserves:
- hidden-bus behavior (no RPC and no cursor move while hidden),
- exact bus gap first/last row semantics,
- animation polling even when the bus panel is hidden,
- max event ID cursor advancement,
- prime-only first animation poll,
- one tree refresh per event batch,
- current ordering of events within a batch.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from theater.client import DaemonClient
from theater.config import RegieSection
from theater.regie.render.layout import Key

logger = logging.getLogger("theater.regie")

#: The type of one rendered tree line, matching ``render_tree``'s output.
type TreeLine = tuple[Any, dict, Key, str, str]


@dataclass
class TreeRefreshResult:
    """Result of a tree poll: the new lines, or None when the poll failed."""

    lines: list[TreeLine] | None = None


@dataclass
class BusRefreshResult:
    """Result of a bus poll: rows to display, gap info, and the new cursor.

    ``rows`` is None when the poll was skipped (hidden) or failed. ``gap`` is
    the number of dropped events when the daemon buffer wrapped, or 0.
    ``new_cursor`` is the updated bus cursor (unchanged when skipped/failed).
    """

    rows: list[dict] | None = None
    gap: int = 0
    new_cursor: int = 0


@dataclass
class AnimRefreshResult:
    """Result of an animation poll, with decisions for the app to execute.

    ``rows`` is the raw batch (possibly empty). ``primed`` is True when this
    poll completed priming (first poll only takes the cursor). ``needs_tree``
    is True when any row requires a tree refresh before animation. ``new_cursor``
    is the updated animation cursor. ``events`` are the per-row animation
    decisions in arrival order.
    """

    rows: list[dict] = field(default_factory=list)
    primed: bool = False
    needs_tree: bool = False
    new_cursor: int = 0
    events: list[AnimEvent] = field(default_factory=list)


@dataclass
class AnimEvent:
    """One animation decision derived from a single bus row."""

    kind: str  # "send", "await_start", "await_end", "skip"
    from_id: str | None = None
    to_id: str | None = None
    token: str | None = None
    handle: str | None = None


class PollingController:
    """Owns bus/anim cursors and animation priming; receives client per call.

    The app constructs this with the loaded ``RegieSection`` for interval/batch
    settings and passes its ``DaemonClient`` to each poll method. The controller
    never touches Textual widgets, reactives, or RichLog.
    """

    def __init__(self, regie: RegieSection) -> None:
        self._regie = regie
        self.bus_cursor: int = 0
        # A separate cursor lets animation continue without consuming hidden bus rows.
        self.anim_cursor: int = 0
        #: Whether the animation poll has seen the log once (prime-only first).
        self._anim_primed: bool = False

    async def poll_tree(
        self,
        cwd_segments: int,
        client: DaemonClient,
        renderer: Callable[..., list[TreeLine]],
    ) -> TreeRefreshResult:
        """Fetch the participant tree and unmanaged panes, render to lines."""
        try:
            tree = await client.call("participants.tree")
            assert isinstance(tree, list)
            unmanaged = await client.call("participants.unmanaged")
            assert isinstance(unmanaged, list)
        except Exception as exc:
            logger.debug("tree refresh failed: %s", exc)
            return TreeRefreshResult(lines=None)
        lines = renderer(tree, unmanaged, cwd_segments=cwd_segments)
        return TreeRefreshResult(lines=lines)

    async def poll_bus(self, bus_visible: bool, client: DaemonClient) -> BusRefreshResult:
        """Poll the bus for panel display. Skips entirely when hidden."""
        if not bus_visible:
            return BusRefreshResult(rows=None, new_cursor=self.bus_cursor)
        try:
            rows = await client.call(
                "bus.tail", limit=self._regie.bus_batch, after_id=self.bus_cursor
            )
            assert isinstance(rows, list)
        except Exception as exc:
            logger.debug("bus refresh failed: %s", exc)
            return BusRefreshResult(rows=None, new_cursor=self.bus_cursor)
        if not rows:
            return BusRefreshResult(rows=None, new_cursor=self.bus_cursor)
        gap = 0
        if rows[0]["id"] > self.bus_cursor + 1 and self.bus_cursor > 0:
            gap = rows[0]["id"] - self.bus_cursor - 1
        new_cursor = rows[-1]["id"]
        return BusRefreshResult(rows=rows, gap=gap, new_cursor=new_cursor)

    async def poll_anim(self, client: DaemonClient) -> AnimRefreshResult:
        """Poll the bus for animation events, hidden panel or not."""
        try:
            rows = await client.call(
                "bus.tail", limit=self._regie.bus_batch, after_id=self.anim_cursor
            )
            assert isinstance(rows, list)
        except Exception as exc:
            logger.debug("tree route animation poll failed: %s", exc)
            return AnimRefreshResult(new_cursor=self.anim_cursor)
        if rows:
            self.anim_cursor = max(int(row["id"]) for row in rows)
        if not self._anim_primed:
            self._anim_primed = True
            return AnimRefreshResult(rows=rows, primed=True, new_cursor=self.anim_cursor)
        needs_tree = any(self._needs_tree_refresh(row) for row in rows)
        events = [self._classify(row) for row in rows]
        return AnimRefreshResult(
            rows=rows, needs_tree=needs_tree, new_cursor=self.anim_cursor, events=events
        )

    @staticmethod
    def _is_prompted_spawn(row: dict) -> bool:
        """Whether row is a child created with a prompt from a visible parent."""
        payload = row.get("payload") or {}
        return bool(
            row.get("kind") == "participant.created"
            and row.get("from_id")
            and payload.get("has_prompt") is True
        )

    @classmethod
    def _needs_tree_refresh(cls, row: dict) -> bool:
        """Whether row animates something the current render may not hold yet."""
        return row.get("kind") == "job.await.start" or cls._is_prompted_spawn(row)

    @staticmethod
    def _classify(row: dict) -> AnimEvent:
        """Turn one bus row into an animation decision."""
        payload = row.get("payload") or {}
        kind = row.get("kind")
        if kind == "agent.send" or PollingController._is_prompted_spawn(row):
            return AnimEvent(
                kind="send",
                from_id=row.get("from_id"),
                to_id=row.get("to_id"),
            )
        if kind == "job.await.start":
            return AnimEvent(
                kind="await_start",
                from_id=row.get("from_id"),
                to_id=row.get("to_id"),
                token=payload.get("token"),
                handle=payload.get("handle"),
            )
        if kind == "job.await.end":
            return AnimEvent(
                kind="await_end",
                from_id=row.get("from_id"),
                to_id=row.get("to_id"),
                token=payload.get("token"),
                handle=payload.get("handle"),
            )
        return AnimEvent(kind="skip")
