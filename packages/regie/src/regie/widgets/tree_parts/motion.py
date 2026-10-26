"""Row motion for the participant tree: reveals, retirements, sweeps and the attention breath."""

from __future__ import annotations

import time
from collections.abc import Collection, Mapping

from textual.widget import Widget

from regie.marks import MarkKind, RowMark
from regie.motions.retirement import LeafRetirementController, LeafRetirementFrame
from regie.motions.reveal import LeafRevealController
from regie.motions.wipe import status_wipe_color
from regie.render.layout import Key
from regie.ui_constants import (
    REGIE_ATTENTION_PULSE_INTERVAL,
    REGIE_ATTENTION_PULSE_LIMIT,
    REGIE_STARTUP_REVEAL_INTERVAL_SECONDS,
    REGIE_WIPE_DEATH_SECONDS,
    REGIE_WIPE_INTERVAL_SECONDS,
)
from regie.widgets.chrome import EmptyTreeState
from regie.widgets.leaf import AgentLeaf
from regie.widgets.spawn_leaf import SpawnLeaf
from regie.widgets.tree_parts._host import _TreeBase
from theater.frontend import StateProjection

#: The sweep that announces a mark arriving on a row.
_MARK_WIPES: dict[MarkKind, tuple[str, float | None]] = {
    MarkKind.TERMINATING: ("$error", REGIE_WIPE_DEATH_SECONDS),
    MarkKind.SENT: ("$success", None),
    MarkKind.BLOCKED: ("$warning", None),
    MarkKind.UNCERTAIN: ("$warning", None),
    MarkKind.FAILED: ("$error", None),
}


class TreeMotion(_TreeBase):
    """Animate rows arriving, leaving, changing and waiting; the widget holds the timers."""

    #: Breathing steps of the attention bar (dim, steady, bold), in order.
    _ATTENTION_BREATH = (-1, 0, 1, 0)

    def configure(self, *, startup_reveal: bool) -> None:
        if self._leaf_reveal.started:
            return
        self._leaf_reveal = LeafRevealController(enabled=startup_reveal and self._motion)
        self._leaf_retirement = LeafRetirementController(enabled=startup_reveal and self._motion)

    def leaf_keys(self) -> tuple[Key, ...]:
        return tuple(
            key
            for key, widget in self._key_widgets.items()
            if isinstance(widget, AgentLeaf | EmptyTreeState)
        )

    def reveal_widths(self, keys: Collection[Key]) -> dict[Key, int]:
        selected = set(keys)
        return {
            key: widget.required_reveal_width
            for key, widget in self._key_widgets.items()
            if key in selected and isinstance(widget, AgentLeaf | EmptyTreeState)
        }

    def set_reveals(self, reveals: dict[Key, int]) -> None:
        self._reveals = dict(reveals)
        for key, widget in self._key_widgets.items():
            if isinstance(widget, AgentLeaf | EmptyTreeState):
                widget.set_reveal(self._reveals.get(key))

    def _sync_reveal(self) -> None:
        keys = self.leaf_keys()
        if not self._leaf_reveal.needs_sync(keys):
            return
        requested = self._leaf_reveal.sync_keys(keys)
        frame = self._leaf_reveal.observe(
            self.reveal_widths(requested),
            animate_new=self._animate_new,
        )
        self.set_reveals(frame.widths)
        if frame.active and self._leaf_reveal_timer is None:
            self._leaf_reveal_timer = self.set_interval(
                REGIE_STARTUP_REVEAL_INTERVAL_SECONDS, self._tick_reveal
            )

    def _tick_reveal(self) -> None:
        keys = self.leaf_keys()
        frame = self._leaf_reveal.tick(self.reveal_widths(self._leaf_reveal.sync_keys(keys)))
        self.set_reveals(frame.widths)
        if not frame.active:
            self._stop_reveal()

    def _stop_reveal(self) -> None:
        if self._leaf_reveal_timer is not None:
            self._leaf_reveal_timer.stop()
            self._leaf_reveal_timer = None

    def _sync_retirement(self, projection: StateProjection) -> None:
        current = {
            ("p", participant.participant_id): bool(participant.parent_id)
            for participant in projection.participants.values()
        }
        # A row gone from the projection stays until its sweep has ended, even if it is already
        # dead: the sweep is what tells the human what happened.
        departed = {key for key in self._key_widgets if key[0] == "p" and key not in current}
        for key in departed & self._kills:
            if not self._wipes.has(key):
                # A kill too quick for its mark to be seen.
                self._wipes.start(key, "$error", seconds=REGIE_WIPE_DEATH_SECONDS)
        self._kills -= departed
        held = {key for key in departed if self._wipes.has(key)}
        self._wipes.cancel(departed - held)
        self._ensure_wipe_timer()
        returned = {key for key in self._held if key in current}
        change = self._leaf_retirement.observe(current)
        self._leaf_reveal.cancel(change.retire)
        if not self._leaf_reveal.active:
            self._stop_reveal()
        self.restore_retiring(change.restore | returned)
        if (change.restore or returned) and not self._leaf_retirement.active:
            self._stop_retirement()
        if held:
            for key, width in self.retire(held).items():
                self._held[key] = (key in change.retire, width)
        if change.retire - held:
            frame = self._leaf_retirement.begin(
                self.retire(change.retire - held), candidates=change.retire - held
            )
            self._apply_retirement(frame)

    def _release_held(self, key: Key) -> None:
        """Its sweep is over: the row now retires, with the reverse reveal if it may, or goes."""
        if key not in self._held:  # a row retiring on its own is not ours to cut short
            return
        animate, width = self._held.pop(key)
        widget = self._retiring.get(key)
        if widget is None:
            return
        if animate:
            self._apply_retirement(self._leaf_retirement.begin({key: width}, candidates={key}))
            return
        self._retiring.pop(key)
        self._retiring_predecessors.pop(key, None)
        self._remove_widget(widget)
        if not self._lines_data and not self._retiring:
            self._reconcile_empty()

    def retire(self, keys: Collection[Key]) -> dict[Key, int]:
        candidates = set(keys)
        retiring: list[tuple[Key, Key | None]] = []
        previous: Key | None = None
        for _, _, key, _, _ in self._lines_data:
            if key in candidates:
                retiring.append((key, previous))
            previous = key
        seen = {key for key, _ in retiring}
        retiring.extend((key, None) for key in candidates - seen)
        widths: dict[Key, int] = {}
        for key, predecessor in retiring:
            widget = self._key_widgets.pop(key, None)
            if not isinstance(widget, AgentLeaf):
                continue
            self._reveals.pop(key, None)
            self._overlaid.discard(key)
            widget.retire()
            self._retiring[key] = widget
            self._retiring_predecessors[key] = predecessor
            widths[key] = widget.visible_reveal_width
        return widths

    def restore_retiring(self, keys: Collection[Key]) -> None:
        for key in keys:
            self._held.pop(key, None)
            widget = self._retiring.pop(key, None)
            if widget is not None:
                self._retiring_predecessors.pop(key, None)
                widget.set_reveal(None)
                self._key_widgets[key] = widget

    def announce_kill(self, participant_id: str) -> None:
        """A kill was asked for: the row will leave with the reverse reveal, roots included, and
        a red sweep first if its termination mark is never seen."""
        key = ("p", participant_id)
        self._leaf_retirement.allow(key)
        self._kills.add(key)

    def _apply_retirement(self, frame: LeafRetirementFrame) -> None:
        for key, reveal in frame.widths.items():
            widget = self._retiring.get(key)
            if widget is not None:
                widget.set_reveal(reveal)
        for key in frame.completed:
            widget = self._retiring.pop(key, None)
            if widget is not None:
                self._retiring_predecessors.pop(key, None)
                self._remove_widget(widget)
        if frame.active and self._leaf_retirement_timer is None:
            self._leaf_retirement_timer = self.set_interval(
                REGIE_STARTUP_REVEAL_INTERVAL_SECONDS, self._tick_retirement
            )
        elif not frame.active:
            self._stop_retirement()
            if not self._lines_data:
                self._reconcile_empty()

    def _tick_retirement(self) -> None:
        self._apply_retirement(self._leaf_retirement.tick())

    def _stop_retirement(self) -> None:
        if self._leaf_retirement_timer is not None:
            self._leaf_retirement_timer.stop()
            self._leaf_retirement_timer = None

    def _observe_statuses(self, projection: StateProjection) -> None:
        """Sweep a row whose status changed; a row seen for the first time is the reveal's."""
        current = {
            ("p", participant.participant_id): participant.status
            for participant in projection.participants.values()
        }
        for key, status in current.items():
            previous = self._statuses.get(key)
            if previous is None or previous == status:
                continue
            if (color := status_wipe_color(previous, status)) is not None:
                # A row folded out of sight cannot sweep: the one standing for it does, but only
                # for what needs the human.
                target = self._hidden_owner.get(key, key)
                if target == key or status in {"awaiting_input", "dead"}:
                    self._wipes.start(
                        target,
                        color,
                        seconds=REGIE_WIPE_DEATH_SECONDS if status == "dead" else None,
                    )
        now = time.monotonic()
        self._attention_since = {
            key: self._attention_since.get(key, now)
            for key, status in current.items()
            if status == "awaiting_input"
        }
        self._statuses = current
        self._ensure_wipe_timer()

    def _observe_marks(self, marks: Mapping[str, RowMark]) -> None:
        """Sweep a row whose operation just produced something worth announcing.

        A spawn ends green; a kill is red; a delivery green; a refusal yellow. (A spawn begins
        cyan, but that is the spawn row opening, not a mark.)
        """
        previous = self._mark_kinds
        # What the first projection already carries happened before the human looked.
        if previous is not None:
            for participant_id, mark in marks.items():
                if mark.kind is MarkKind.TERMINATING:
                    self._leaf_retirement.allow(("p", participant_id))
                if previous.get(participant_id) is not mark.kind and (
                    wipe := _MARK_WIPES.get(mark.kind)
                ):
                    self._wipes.start(("p", participant_id), wipe[0], seconds=wipe[1])
            for participant_id, kind in previous.items():
                # Up: a rolled-back spawn has no row left to sweep.
                if (
                    kind is MarkKind.LAUNCHING
                    and participant_id not in marks
                    and ("p", participant_id) in self._statuses
                ):
                    self._wipes.start(("p", participant_id), "$success")
        self._mark_kinds = {participant_id: mark.kind for participant_id, mark in marks.items()}
        self._ensure_wipe_timer()

    def _ensure_wipe_timer(self) -> None:
        if self._wipes.active and self._wipe_timer is None:
            self._wipe_timer = self.set_interval(REGIE_WIPE_INTERVAL_SECONDS, self._tick_wipe)

    def flash(self, participant_id: str, color: str) -> None:
        """Sweep one row in *color* to show something just landed on it."""
        key = ("p", participant_id)
        if key in self._key_widgets:
            self._wipes.start(key, color)
            self._ensure_wipe_timer()

    def _tick_wipe(self) -> None:
        frame = self._wipes.tick()
        for key, wipe in frame.running.items():
            if isinstance(widget := self._sweepable(key), AgentLeaf | SpawnLeaf):
                widget.set_wipe(wipe)
        for key in frame.finished:
            if isinstance(widget := self._sweepable(key), AgentLeaf | SpawnLeaf):
                widget.set_wipe(None)
            self._release_held(key)
        if not frame.active and self._wipe_timer is not None:
            self._wipe_timer.stop()
            self._wipe_timer = None

    def _sweepable(self, key: Key) -> Widget | None:
        """The row a sweep draws on: a held row is no longer in the tree but is still drawn."""
        return self._key_widgets.get(key) or self._retiring.get(key)

    def _sync_attention(self) -> None:
        """Breathe the newest few waiting rows; the rest hold the steady tint."""
        if not self._attention_since or not self._motion:
            self._stop_attention()
            return
        if self._attention_timer is None:
            self._attention_timer = self.set_interval(
                REGIE_ATTENTION_PULSE_INTERVAL, self._tick_attention
            )

    def _tick_attention(self) -> None:
        self._attention_phase = (self._attention_phase + 1) % len(self._ATTENTION_BREATH)
        newest = sorted(self._attention_since, key=self._attention_since.__getitem__, reverse=True)
        breathing = set(newest[:REGIE_ATTENTION_PULSE_LIMIT])
        level = self._ATTENTION_BREATH[self._attention_phase]
        for key in self._attention_since:
            if isinstance(widget := self._key_widgets.get(key), AgentLeaf):
                widget.set_attention_level(level if key in breathing else 0)

    def _stop_attention(self) -> None:
        if self._attention_timer is not None:
            self._attention_timer.stop()
            self._attention_timer = None

    def _stop_motion(self) -> None:
        """Stop every animation timer and forget rows still on screen only for their motion."""
        self._stop_reveal()
        self._stop_retirement()
        self._stop_attention()
        self._held.clear()
        if self._wipe_timer is not None:
            self._wipe_timer.stop()
            self._wipe_timer = None
        self._leaf_retirement.clear()
