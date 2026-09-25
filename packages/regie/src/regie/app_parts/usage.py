"""Usage footer: totals, metric tiles, and the breakdown panel."""

from __future__ import annotations

from textual.dom import DOMNode

from regie.app_parts._shared import _AppBase
from regie.controllers.usage import ActivateOutcome, FetchAccept, SyncOutcome, participant_costs
from regie.formatting import participant_label
from regie.ui_constants import (
    REGIE_COST_WINDOW_LABELS,
    REGIE_MICROCENTS_PER_DOLLAR,
    REGIE_USAGE_AVERAGE_WINDOW_DAYS,
)
from regie.widgets import (
    ParticipantTree,
    PriceFooter,
    StatsFooter,
    TreeStack,
    UsageBreakdownPanel,
    UsageMetricTile,
    UsagePeriodBar,
)
from theater.frontend import (
    FrontendClientError,
    FrontendResponseError,
    FrontendTransportError,
)


class UsageFooter(_AppBase):
    async def _refresh_usage(self) -> None:
        window = self._cost_window()
        projection = self._state.projection
        participant_ids = tuple(projection.participants) if projection is not None else ()
        try:
            usage = await self._usage.refresh(window=window, participant_ids=participant_ids)
        except (FrontendClientError, FrontendResponseError, FrontendTransportError, TypeError):
            return
        if not self._view_active:
            return
        participant_result = dict(usage.by_participant)
        names = (
            {
                participant_id: participant_label(participant)
                for participant_id, participant in projection.participants.items()
            }
            if projection is not None
            else {}
        )
        self._usage_panel.update_participants(participant_result, names=names)
        self.query_one(ParticipantTree).set_usage_costs(participant_costs(participant_result))
        summary = dict(usage.summary)
        windowed = summary.get("windowed")
        average = summary.get("average")
        totals = dict(windowed) if isinstance(windowed, dict) else dict(usage.totals)
        self.query_one("#stats-footer", StatsFooter).totals = totals
        self.query_one("#price-footer", PriceFooter).totals = totals
        if isinstance(average, dict):
            active_days = average.get("active_days", REGIE_USAGE_AVERAGE_WINDOW_DAYS)
            try:
                days = float(active_days)
                daily_average = (
                    float(average.get("cost_microcents", 0)) / REGIE_MICROCENTS_PER_DOLLAR / days
                    if days > 0
                    else 0.0
                )
            except (TypeError, ValueError):
                daily_average = 0.0
            self.query_one("#price-footer", PriceFooter).daily_avg = daily_average
        self.query_one("#usage-period", UsagePeriodBar).period_label = REGIE_COST_WINDOW_LABELS[
            window
        ]
        self._render_usage_breakdown()

    def _cost_window(self) -> str:
        window = self.settings.cost_window
        return window if window in REGIE_COST_WINDOW_LABELS else "day"

    def on_usage_metric_tile_hovered(self, message: UsageMetricTile.Hovered) -> None:
        self._usage_panel.pointer_metric = message.metric
        self._sync_usage_metric()

    def on_usage_metric_tile_clicked(self, message: UsageMetricTile.Clicked) -> None:
        self._usage_panel.pointer_metric = message.metric
        self._sync_usage_metric()
        self._toggle_usage_detailed()

    def on_usage_metric_tile_left(self, _message: UsageMetricTile.Left) -> None:
        self.call_after_refresh(self._hide_usage_breakdown_if_unhovered)

    def on_usage_breakdown_panel_left(self, _message: UsageBreakdownPanel.Left) -> None:
        self.call_after_refresh(self._hide_usage_breakdown_if_unhovered)

    def _render_usage_breakdown(self) -> None:
        metric = self._usage_panel.active_metric
        if metric is None or not self.is_running:
            return
        result = (
            self._usage_panel.detailed_breakdown
            if self._usage_panel.detailed
            else self._usage_panel.breakdown
        )
        message = (
            self._usage_panel.detailed_message
            if self._usage_panel.detailed
            else self._usage_panel.message
        )
        self.query_one("#usage-breakdown", UsageBreakdownPanel).render_state(
            metric,
            result=result,
            message=message,
            detailed=self._usage_panel.detailed,
            participants=self._usage_panel.participant_usage,
        )

    def _activate_usage_metric(self, metric: str) -> None:
        outcome = self._usage_panel.activate(metric)
        if outcome is not ActivateOutcome.NO_CHANGE:
            for tile in self.query(UsageMetricTile):
                tile.set_class(tile.metric == metric, "-hot")
        panel = self.query_one("#usage-breakdown", UsageBreakdownPanel)
        panel.set_class(True, "-visible")
        if outcome is ActivateOutcome.FIRST_OPEN:
            self._constrain_usage_breakdown()
            self._usage_panel.begin_first_open()
        if outcome in {ActivateOutcome.FIRST_OPEN, ActivateOutcome.SWITCH}:
            if self._usage_panel.detailed:
                self._ensure_detailed_usage_fetch()
            else:
                self._ensure_compact_usage_fetch()
        self._render_usage_breakdown()

    def _ensure_compact_usage_fetch(self) -> None:
        generation = self._usage_panel.begin_compact_fetch()
        if generation is not None:
            self.run_worker(self._fetch_compact_usage(generation), exclusive=False)

    def _ensure_detailed_usage_fetch(self) -> None:
        generation = self._usage_panel.begin_detailed_fetch()
        if generation is not None:
            self.run_worker(self._fetch_detailed_usage(generation), exclusive=False)

    def _sync_usage_metric(self) -> None:
        outcome = self._usage_panel.sync()
        if outcome is SyncOutcome.ACTIVATE:
            metric = self._usage_panel.resolve_metric()
            assert metric is not None
            self._activate_usage_metric(metric)
            return
        if outcome is SyncOutcome.CLOSE:
            for tile in self.query(UsageMetricTile):
                tile.set_class(False, "-hot")
            self._usage_panel.clear_active()
            self.query_one("#usage-breakdown", UsageBreakdownPanel).set_class(False, "-visible")

    def _toggle_usage_detailed(self) -> None:
        if self._usage_panel.active_metric is None:
            return
        self._usage_panel.toggle_detailed()
        self._render_usage_breakdown()
        if self._usage_panel.detailed:
            self._ensure_detailed_usage_fetch()
        else:
            self._ensure_compact_usage_fetch()

    async def _fetch_compact_usage(self, generation: int) -> None:
        try:
            result = await self._usage.breakdown()
        except (
            FrontendClientError,
            FrontendResponseError,
            FrontendTransportError,
            TypeError,
        ) as exc:
            accepted = self._usage_panel.accept_fetch(
                generation=generation,
                result=None,
                message=f"usage unavailable: {exc}",
            )
        else:
            accepted = self._usage_panel.accept_fetch(
                generation=generation, result=result, message=None
            )
        if accepted is FetchAccept.ACCEPTED and not self._usage_panel.detailed:
            self._render_usage_breakdown()

    async def _fetch_detailed_usage(self, generation: int) -> None:
        try:
            result = await self._usage.detailed_breakdown()
        except (FrontendClientError, FrontendResponseError, FrontendTransportError, TypeError):
            accepted = self._usage_panel.accept_detailed_fetch(
                generation=generation,
                result=self._usage_panel.breakdown,
                message="model details unavailable",
            )
        else:
            accepted = self._usage_panel.accept_detailed_fetch(
                generation=generation, result=result, message=None
            )
        if accepted is FetchAccept.ACCEPTED and self._usage_panel.detailed:
            self._render_usage_breakdown()

    def _constrain_usage_breakdown(self) -> None:
        panel = self.query_one("#usage-breakdown", UsageBreakdownPanel)
        panel.constrain_to_height(self.query_one("#tree-stack", TreeStack).size.height)

    def _hide_usage_breakdown_if_unhovered(self) -> None:
        node: DOMNode | None = self.mouse_over
        while node is not None:
            if isinstance(node, UsageMetricTile):
                self._usage_panel.pointer_metric = node.metric
                self._sync_usage_metric()
                return
            if isinstance(node, UsageBreakdownPanel):
                return
            node = node.parent
        self._usage_panel.pointer_metric = None
        self._sync_usage_metric()

    def _select_usage_metric(self, metric: str, *, origin: str | None = None) -> None:
        self._usage_panel.keyboard_metric = metric
        self._usage_panel.keyboard_origin = origin
        self.query_one(ParticipantTree).set_cursor_visible(False)
        self._sync_usage_metric()

    def _show_usage_visibility(self) -> None:
        for footer in (UsagePeriodBar, StatsFooter, PriceFooter):
            self.query_one(footer).display = self._usage_visible
        if not self._usage_visible:
            # A hidden footer can neither hold keyboard focus nor keep its breakdown open.
            if self._usage_panel.in_footer:
                self._leave_usage_metrics()
            self._usage_panel.pointer_metric = None
            self._sync_usage_metric()

    def action_toggle_usage(self) -> None:
        self._usage_visible = not self._usage_visible
        self._show_usage_visibility()

    def _leave_usage_metrics(self) -> None:
        self._usage_panel.leave_keyboard()
        self.query_one(ParticipantTree).set_cursor_visible(True)
        self._sync_usage_metric()
