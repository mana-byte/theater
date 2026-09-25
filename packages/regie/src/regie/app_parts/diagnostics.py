"""Diagnostic bus panel and coordination route animations."""

from __future__ import annotations

from collections.abc import Mapping

from rich.text import Text
from textual.widgets import RichLog

from regie.app_parts._shared import _AppBase, logger
from regie.bus_view import format_bus_line
from regie.render.routing import await_highlight_cells
from regie.ui_constants import REGIE_TRACE_ANIM_INTERVAL
from regie.widgets import ParticipantTree
from theater.frontend import (
    FrontendClientError,
    FrontendResponseError,
    FrontendTransportError,
)


class DiagnosticsDisplay(_AppBase):
    async def _refresh_bus(self) -> None:
        if not self._bus_visible:
            return
        try:
            rows = await self._bus.poll()
        except (
            AttributeError,
            FrontendClientError,
            FrontendResponseError,
            FrontendTransportError,
            TypeError,
        ) as exc:
            logger.debug("diagnostic bus unavailable: %s", exc)
            return
        if not self._view_active:
            return
        view = self.query_one("#bus", RichLog)
        if self._bus.last_gap:
            view.write(Text(f"... {self._bus.last_gap} events dropped", style="dim italic"))
        for row in rows:
            variables = self.theme_variables if self.is_running else None
            view.write(format_bus_line(row, variables=variables))

    async def _refresh_animations(self) -> None:
        """Follow coordination events on a cursor independent of the bus panel."""
        try:
            rows = await self._animation_bus.poll()
        except (
            AttributeError,
            FrontendClientError,
            FrontendResponseError,
            FrontendTransportError,
            TypeError,
        ):
            return
        if not self._animation_primed:
            self._animation_primed = True
            return
        if any(self._animation_needs_fresh_tree(row) for row in rows):
            await self._tick_synchronize()
        if not self._view_active:
            return
        for row in rows:
            self._animate_bus_row(row)

    @staticmethod
    def _animation_needs_fresh_tree(row: object) -> bool:
        if not isinstance(row, dict):
            return False
        payload = row.get("payload")
        prompted_spawn = (
            row.get("kind") == "participant.created"
            and row.get("from_id")
            and isinstance(payload, Mapping)
            and payload.get("has_prompt") is True
        )
        return row.get("kind") == "job.await.start" or bool(prompted_spawn)

    def _animate_bus_row(self, row: object) -> None:
        if not isinstance(row, dict):
            return
        kind = row.get("kind")
        from_id = row.get("from_id") if isinstance(row.get("from_id"), str) else None
        to_id = row.get("to_id") if isinstance(row.get("to_id"), str) else None
        payload_value = row.get("payload")
        payload: Mapping[str, object] = payload_value if isinstance(payload_value, Mapping) else {}
        prompted_spawn = (
            kind == "participant.created" and from_id is not None and payload.get("has_prompt")
        )
        if kind in {"agent.send", "agent.steer", "agent.queue_followup"} or prompted_spawn:
            self.start_route_animation(from_id, to_id)
        elif kind == "job.await.start":
            self.start_await_animation(payload.get("token"), payload.get("handle"), from_id, to_id)
        elif kind == "job.await.end":
            self.stop_await_animation(payload.get("token"), payload.get("handle"), from_id, to_id)

    def start_route_animation(self, from_id: str | None, to_id: str | None) -> None:
        tree = self.query_one(ParticipantTree)
        if self._animation.start_route(tree.tree_lines, from_id, to_id).started:
            self._ensure_animation_timer()

    def start_await_animation(
        self,
        token: object,
        handle: object,
        from_id: str | None,
        to_id: str | None,
    ) -> None:
        tree = self.query_one(ParticipantTree)
        if self._animation.start_await(tree.tree_lines, token, handle, from_id, to_id).started:
            self._ensure_animation_timer()

    def stop_await_animation(
        self,
        token: object,
        handle: object,
        from_id: str | None,
        to_id: str | None,
    ) -> None:
        decision = self._animation.stop_await(token, handle, from_id, to_id)
        if decision.clear_overlays:
            self.query_one(ParticipantTree).set_overlays({})
        if decision.stop_timer:
            self._stop_animation_timer()

    def _ensure_animation_timer(self) -> None:
        if self._animation_timer is None:
            self._animation_timer = self.set_interval(
                REGIE_TRACE_ANIM_INTERVAL, self._tick_route_animations
            )

    def _tick_route_animations(self) -> None:
        tree = self.query_one(ParticipantTree)
        result = self._animation.tick(
            tree.tree_lines,
            tree.revision,
            await_highlight_cells,
        )
        tree.set_overlays(result.overlays)
        if result.stop_timer:
            self._stop_animation_timer()

    def _stop_animation_timer(self) -> None:
        if self._animation_timer is not None:
            self._animation_timer.stop()
            self._animation_timer = None

    def _show_bus_visibility(self) -> None:
        self.query_one("#bus", RichLog).display = self._bus_visible

    def action_toggle_bus(self) -> None:
        self._bus_visible = not self._bus_visible
        self._show_bus_visibility()
