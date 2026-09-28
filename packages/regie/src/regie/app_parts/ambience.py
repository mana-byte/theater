"""Run the configured tree ambience while the user is away on a staged surface, or on the tree."""

from __future__ import annotations

from time import monotonic

from textual import events
from textual.timer import Timer

from regie.ambience.driver import AmbienceDriver
from regie.ambience.registry import scene_for
from regie.app_parts._shared import _AppBase
from regie.widgets import ParticipantTree
from regie.widgets.ambience_band import AmbienceBand


class TreeAmbience(_AppBase):
    _ambience: AmbienceDriver | None = None
    _ambience_timer: Timer | None = None
    _ambience_at = 0.0
    #: Once you leave a playing ambience, it rests this long before it may play again.
    _ambience_cooldown = 10.0
    _ambience_on = False
    _ambience_rest_until = 0.0
    _ambience_wake: Timer | None = None

    def _initialize_ambience(self) -> None:
        scene = scene_for(self.settings.tree_ambience)
        self._ambience = None if scene is None else AmbienceDriver(scene)
        self._sync_ambience()

    def _ambience_mounted(self) -> bool:
        return bool(self.query(AmbienceBand)) and bool(self.query(ParticipantTree))

    def _ambience_wanted(self) -> bool:
        if self.settings.tree_ambience_when == "tree":
            return self._tree_has_focus()
        return self._away_on_stage()

    def _away_on_stage(self) -> bool:
        """The user works in a staged terminal (Régie's pane lost focus) or trajectory."""
        return not self.app_focus or self._trajectory_has_focus()

    def _tree_has_focus(self) -> bool:
        """Régie's pane is focused and the keyboard is on the tree, not a staged surface.

        The tree's own inputs (rename, spawn row, command bar) count as the tree.
        """
        if not self.app_focus or self._usage_panel.in_footer or self._trajectory_has_focus():
            return False
        focused = self.focused
        return focused is None or any(node.id == "sidebar" for node in focused.ancestors)

    def _free_band(self) -> tuple[int, int]:
        """The empty rows under the tree's last row; none once the tree scrolls."""
        tree = self.query_one(ParticipantTree)
        band = self.query_one(AmbienceBand)
        width = tree.size.width - band.styles.padding.left - band.styles.padding.right
        # The viewport never reports less than its own height, so sum the rows themselves.
        rows = sum(child.outer_size.height for child in tree.children if child.display)
        return max(0, width), max(0, tree.size.height - rows)

    def _sync_ambience(self) -> None:
        """Feed focus and free space to the driver; tick only while something plays."""
        driver = self._ambience
        # Deferred syncs can land while the app is tearing its widgets down.
        if driver is None or not self.is_running or not self._ambience_mounted():
            return
        driver.set_band(*self._free_band())
        driver.set_active(self._ambience_allowed())
        if driver.running and self._ambience_timer is None:
            self._ambience_at = monotonic()
            self._ambience_timer = self.set_interval(1 / driver.fps, self._tick_ambience)
            self._tick_ambience()
        elif not driver.running:
            self._stop_ambience()

    def _ambience_allowed(self) -> bool:
        """Wanted, and not resting after you left it; a rest ends by re-syncing on a timer."""
        now, wanted = monotonic(), self._ambience_wanted()
        if self._ambience_on and not wanted:
            self._ambience_rest_until = now + self._ambience_cooldown
        resting = wanted and now < self._ambience_rest_until
        if resting and self._ambience_wake is None:
            self._ambience_wake = self.set_timer(
                self._ambience_rest_until - now, self._ambience_woke
            )
        self._ambience_on = wanted and not resting
        return self._ambience_on

    def _ambience_woke(self) -> None:
        self._ambience_wake = None
        self._sync_ambience()

    def _tick_ambience(self) -> None:
        driver = self._ambience
        if driver is None or not self._ambience_mounted():
            return
        now = monotonic()
        dt, self._ambience_at = now - self._ambience_at, now
        width, height = self._free_band()
        driver.set_band(width, height)
        cells = driver.tick(dt)
        if not driver.running:
            self._stop_ambience()
            return
        self.query_one(AmbienceBand).show_cells(cells, width, height)

    def _stop_ambience(self) -> None:
        if self._ambience_timer is not None:
            self._ambience_timer.stop()
            self._ambience_timer = None
        for band in self.query(AmbienceBand):
            band.clear()

    def watch_app_focus(self, _focused: bool) -> None:
        self._sync_ambience()

    def on_descendant_focus(self, _event: events.DescendantFocus) -> None:
        self._sync_ambience()

    def on_descendant_blur(self, _event: events.DescendantBlur) -> None:
        self.call_after_refresh(self._sync_ambience)


__all__ = ["TreeAmbience"]
