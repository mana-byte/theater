"""Initial load of the catalog, public state, and startup readers."""

from __future__ import annotations

import asyncio
from functools import partial

from regie.app_parts._shared import _AppBase, logger
from regie.controllers.startup import start_reader
from regie.dashboard import WelcomeDashboard
from regie.latency import startup_milestone, startup_phase, startup_stage
from regie.ui_constants import REGIE_USAGE_POLL_INTERVAL_SECONDS
from regie.widgets import ParticipantTree
from theater.frontend import (
    FrontendClientError,
    FrontendResponseError,
    FrontendTransportError,
    StateSynchronizationError,
    local_harness_catalog,
)
from theater.frontend.dto.catalogs import HarnessCatalogEntry


class StartupLoading(_AppBase):
    def _start_initial_load(self) -> None:
        if self._closed or self._startup_task is not None:
            return
        startup_milestone("first_frame", self._startup_started_at)
        self._startup_task = asyncio.create_task(self._initialize_ui(), name="regie-startup")
        self._startup_task.add_done_callback(self._initial_load_finished)

    def _initial_load_finished(self, task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._handle_exception(exc)

    async def _initialize_ui(self) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self._load_initial_catalog())
            group.create_task(self._initialize_state_follow())
            group.create_task(
                start_reader(
                    self._initialize_local_projection,
                    interval=self.settings.tree_interval,
                    poll=self._refresh_local_projection,
                    start_timer=self.set_interval,
                )
            )
            for phase, poll, interval, load in (
                ("usage", self._refresh_usage, REGIE_USAGE_POLL_INTERVAL_SECONDS, True),
                ("animations", self._refresh_animations, self.settings.bus_interval, True),
                ("bus", self._refresh_bus, self.settings.bus_interval, self._bus_visible),
            ):
                group.create_task(
                    start_reader(
                        partial(startup_stage, phase, poll) if load else None,
                        interval=interval,
                        poll=poll,
                        start_timer=self.set_interval,
                    )
                )
        self.call_after_refresh(startup_milestone, "ready", self._startup_started_at)

    async def _initialize_state_follow(self) -> None:
        try:
            await self._initialize_projection()
        finally:
            self._projection_ready.set()
        if self._view_active:
            self._state_follow.start()

    async def _initialize_local_projection(self) -> None:
        await self._catalog_ready.wait()
        await self._projection_ready.wait()
        with startup_phase("unmanaged"):
            await self._refresh_local_projection()

    async def _load_initial_catalog(self) -> bool:
        try:
            loaded = await startup_stage("catalog", self._load_catalog)
            if self._projection_ready.is_set() and self._state.projection is not None:
                self._show_projection(self._state.projection)
            return loaded
        finally:
            self._catalog_ready.set()

    async def wait_for_catalog(self) -> None:
        """A palette may wait for startup without owning or cancelling its reads."""
        await self._catalog_ready.wait()

    async def _load_catalog(self) -> bool:
        async with self._catalog_lock:
            try:
                self._harnesses = (await self._clients.catalog.catalogs.harnesses()).value.items
            except (
                FrontendClientError,
                FrontendResponseError,
                FrontendTransportError,
                TypeError,
            ) as exc:
                logger.debug("harness catalog unavailable: %s", exc)
                try:
                    self._harnesses = local_harness_catalog()
                except Exception as fallback_exc:
                    logger.debug("local harness catalog unavailable: %s", fallback_exc)
                    return False
                if self._view_active:
                    self.query_one(WelcomeDashboard).show_catalog(self._installed_harnesses)
                return False
            if self._view_active:
                self.query_one(WelcomeDashboard).show_catalog(self._installed_harnesses)
            return True

    @property
    def _installed_harnesses(self) -> tuple[HarnessCatalogEntry, ...]:
        """Harnesses whose executable the daemon found; the only ones Régie offers.

        The full catalog still names icons for participants of any harness.
        """
        return tuple(entry for entry in self._harnesses if entry.installed)

    async def _initialize_projection(self) -> None:
        try:
            with startup_phase("snapshot"):
                projection = await self._state.initialize()
        except (
            FrontendClientError,
            FrontendResponseError,
            FrontendTransportError,
            StateSynchronizationError,
            TypeError,
        ) as exc:
            self._show_state_error(exc)
            self._finish_initial_projection()
            return
        self._last_state_error = None
        if not self._view_active:
            return
        with startup_phase("projection"):
            self._show_projection(self._state.projection or projection)
        self._render_pending_actions()

    def _finish_initial_projection(self) -> None:
        if not self._view_active:
            return
        self._initial_projection_pending = False
        self.query_one(ParticipantTree).loading = False
        self.refresh_bindings()
