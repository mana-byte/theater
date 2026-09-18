"""The independent public-SDK Régie Textual application."""

from __future__ import annotations

import contextlib
from collections.abc import Iterable

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import RichLog, Static

from regie.bus import DiagnosticBusController
from regie.contracts import PresentationOperations, RegieSettings
from regie.controllers.actions import ActionRecord, OperationController
from regie.controllers.staging import StageController, StageOutcome, StageResult
from regie.formatting import diagnostic_line
from regie.presentation import stageability
from regie.state import StateController
from regie.trajectory import TrajectoryController
from regie.usage import UsageController
from regie.widgets import CatalogDashboard, ParticipantTree
from theater.frontend import (
    FrontendClient,
    FrontendClientError,
    FrontendResponseError,
    FrontendTransportError,
    StateProjection,
    StateSynchronizationError,
)
from theater.frontend.dto.catalogs import HarnessCatalogEntry


class RegieApp(App):
    """A presentation-only operator UI constructed from explicit public collaborators."""

    CSS = """
    Screen { layout: horizontal; }
    #sidebar { width: 52; min-width: 40; }
    #participant-tree { height: 1fr; padding: 1; }
    #bus { height: 12; }
    #dashboard { width: 1fr; padding: 1 2; }
    """

    def __init__(
        self,
        *,
        client: FrontendClient,
        settings: RegieSettings,
        presentation: PresentationOperations,
    ) -> None:
        super().__init__()
        self.client = client
        self.settings = settings
        self.presentation = presentation
        self._state = StateController(client)
        self._actions = OperationController(client)
        self._staging = StageController(settings, presentation)
        self._bus = DiagnosticBusController(client, batch=settings.bus_batch)
        self._trajectory = TrajectoryController(client, page_size=settings.trajectory_page_size)
        self._usage = UsageController(client)
        self._harnesses: tuple[HarnessCatalogEntry, ...] = ()

    @property
    def projection(self) -> StateProjection | None:
        return self._state.projection

    @property
    def actions(self) -> OperationController:
        return self._actions

    def capability_reason(self, participant_id: str, action: str) -> str | None:
        """Return the current public capability reason a button should display when disabled."""
        projection = self._state.projection
        if projection is None:
            return "orchestration state has not loaded"
        if projection.stale:
            return "orchestration state is stale; wait for a fresh snapshot"
        participant = projection.participants.get(participant_id)
        if participant is None:
            return "participant is not in the active public projection"
        capability = participant.actions.get(action)
        if capability is None:
            return "action is not advertised by the public projection"
        if capability.supported and capability.route_available and capability.admissible:
            return None
        return capability.reason or capability.detail or "action is currently unavailable"

    def _control_refusal(self, participant_id: str, action: str) -> ActionRecord | None:
        reason = self.capability_reason(participant_id, action)
        if reason is None:
            return None
        return self._actions.refuse_locally(action, participant_id, reason)

    def compose(self) -> ComposeResult:
        with Vertical(id="sidebar"):
            yield ParticipantTree(id="participant-tree")
            yield RichLog(id="bus", max_lines=200, wrap=False)
        with Horizontal(id="dashboard"):
            yield CatalogDashboard(id="catalog-dashboard")
            with Vertical():
                yield Static("Public API connected on demand", id="state-status")
                yield Static("usage unavailable", id="usage")

    async def on_mount(self) -> None:
        self.query_one("#sidebar").styles.width = self.settings.sidebar_width
        if self.settings.theme and self.settings.theme in self.available_themes:
            self.theme = self.settings.theme
        elif self.settings.theme:
            self.notify(f"unknown theme {self.settings.theme!r}", severity="warning")
        if self.settings.cost_window not in {"day", "week", "month", "year"}:
            self.notify(
                f"unknown cost window {self.settings.cost_window!r}; using 'day'",
                severity="warning",
            )
        await self._load_catalog()
        await self._refresh_usage()
        await self._initialize_projection()
        self.set_interval(self.settings.tree_interval, self._synchronize_projection)
        if self.settings.bus_visible:
            self.set_interval(self.settings.bus_interval, self._refresh_bus)

    async def _load_catalog(self) -> None:
        try:
            self._harnesses = (await self.client.catalogs.harnesses()).value.items
        except (FrontendClientError, FrontendResponseError, FrontendTransportError) as exc:
            self.notify(f"harness catalog unavailable: {exc}", severity="warning")
            return
        self.query_one(CatalogDashboard).show_harnesses(self._harnesses)

    async def _initialize_projection(self) -> None:
        try:
            projection = await self._state.initialize()
        except (
            FrontendClientError,
            FrontendResponseError,
            FrontendTransportError,
            StateSynchronizationError,
        ) as exc:
            self._show_state_error(exc)
            return
        self._show_projection(projection)

    async def _refresh_usage(self) -> None:
        try:
            usage = await self._usage.refresh(window=self.settings.cost_window)
        except (FrontendClientError, FrontendResponseError, FrontendTransportError) as exc:
            self.query_one("#usage", Static).update(f"usage unavailable: {exc}")
            return
        self.query_one("#usage", Static).update(str(dict(usage.totals)))

    async def _synchronize_projection(self) -> None:
        previous = self._state.projection
        was_stale = previous is not None and previous.stale
        try:
            projection = await self._state.synchronize()
        except (
            FrontendClientError,
            FrontendResponseError,
            FrontendTransportError,
            StateSynchronizationError,
        ) as exc:
            self._show_state_error(exc)
            stale_projection = self._state.projection
            if stale_projection is not None:
                self._show_projection(stale_projection)
            return
        if was_stale and not projection.stale:
            await self._actions.refresh_pending()
        self._show_projection(projection)

    async def _refresh_bus(self) -> None:
        try:
            rows = await self._bus.poll()
        except (FrontendClientError, FrontendResponseError, FrontendTransportError) as exc:
            self.notify(f"diagnostic bus unavailable: {exc}", severity="warning")
            return
        view = self.query_one("#bus", RichLog)
        for row in rows:
            view.write(diagnostic_line(row))

    def _show_projection(self, projection: StateProjection) -> None:
        stage_reasons = {
            participant.participant_id: eligibility.reason
            for participant in projection.participants.values()
            if not (
                eligibility := stageability(participant, projection.providers, self.presentation)
            ).allowed
            and eligibility.reason is not None
        }
        self.query_one(ParticipantTree).show_projection(
            projection,
            participant_detail=self.settings.participant_detail,
            cwd_segments=self.settings.cwd_segments,
            stage_reasons=stage_reasons,
        )
        status = "stale — reconnecting" if projection.stale else "live"
        self.query_one("#state-status", Static).update(status)

    def _show_state_error(self, exc: Exception) -> None:
        projection = self._state.projection
        state = "stale" if projection is not None else "unavailable"
        self.query_one("#state-status", Static).update(f"{state}: {exc}")

    async def stage_participant(
        self,
        participant_id: str,
        *,
        target_window: str,
    ) -> StageResult | None:
        projection = self._state.projection
        if projection is None:
            return None
        if projection.stale:
            return StageResult(
                StageOutcome.UNAVAILABLE,
                None,
                "orchestration state is stale; wait for a fresh snapshot",
            )
        participant = projection.participants.get(participant_id)
        if participant is None:
            return None
        return await self._staging.stage(
            participant,
            projection.providers,
            target_window=target_window,
        )

    async def submit_send(self, participant_id: str, prompt: str) -> ActionRecord:
        if refusal := self._control_refusal(participant_id, "send"):
            return refusal
        return await self._actions.send(participant_id, prompt)

    async def submit_steer(self, participant_id: str, prompt: str) -> ActionRecord:
        if refusal := self._control_refusal(participant_id, "steer"):
            return refusal
        return await self._actions.steer(participant_id, prompt)

    async def submit_followup(self, participant_id: str, prompt: str) -> ActionRecord:
        if refusal := self._control_refusal(participant_id, "queue_followup"):
            return refusal
        return await self._actions.queue_followup(participant_id, prompt)

    async def submit_interrupt(self, participant_id: str) -> ActionRecord:
        if refusal := self._control_refusal(participant_id, "interrupt"):
            return refusal
        return await self._actions.interrupt(participant_id)

    async def submit_settings(
        self,
        participant_id: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ActionRecord:
        if refusal := self._control_refusal(participant_id, "settings_update"):
            return refusal
        return await self._actions.update_settings(
            participant_id,
            model=model,
            reasoning_effort=reasoning_effort,
        )

    async def submit_termination(self, participant_id: str) -> ActionRecord:
        return await self._actions.terminate(participant_id)

    async def submit_spawn(
        self,
        harness: str,
        prompt: str,
        approval: str,
    ) -> ActionRecord:
        return await self._actions.spawn(harness, prompt, approval)

    async def retry_action(self, action: str, target_id: str) -> ActionRecord | None:
        """Retry only a user-confirmed uncertain action with its retained idempotency key."""
        return await self._actions.retry(action, target_id)

    async def open_trajectory(self, participant_id: str) -> None:
        await self._trajectory.open(participant_id)

    async def on_unmount(self) -> None:
        await self._actions.close()
        with contextlib.suppress(Exception):
            await self._trajectory.close()
        with contextlib.suppress(Exception):
            await self._staging.close()
        with contextlib.suppress(Exception):
            await self.client.close()


def catalog_names(harnesses: Iterable[HarnessCatalogEntry]) -> tuple[str, ...]:
    """Expose public catalog names for palette adapters without local discovery."""
    return tuple(entry.name for entry in harnesses if entry.launch_available)


__all__ = ["RegieApp", "catalog_names"]
