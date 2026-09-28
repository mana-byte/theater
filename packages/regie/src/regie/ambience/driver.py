"""The phase machine shared by every scene: intro when wanted, idle while wanted, outro after.

An intro or outro, once begun, always plays to its end; a change of mind waits for it.

Pure and clock-free: the caller passes elapsed time, so it is tested without Textual.
"""

from __future__ import annotations

import random

from regie.ambience.scene import MAX_TRANSITION_SECONDS, Cell, Phase, Scene


class AmbienceDriver:
    """Run one scene's intro/idle/outro against the tree's focus and the band's free rows."""

    def __init__(self, scene_type: type[Scene], *, seed: int | None = None) -> None:
        self._scene_type = scene_type
        self._seed = seed
        self._scene: Scene | None = None
        self.phase: Phase | None = None
        self._progress = 0.0
        self._active = False
        self._size = (0, 0)

    @property
    def fps(self) -> float:
        return self._scene_type.fps

    @property
    def running(self) -> bool:
        """Whether frames are still needed: something is playing or about to."""
        return self.phase is not None

    def _fits(self) -> bool:
        width, height = self._size
        return width > 0 and height >= self._scene_type.min_rows

    def set_active(self, active: bool) -> None:
        """Wanted or not; a transition in flight finishes first, then the other one follows."""
        if active == self._active:
            return
        self._active = active
        if active and self.phase is None and self._fits():
            self._start()
        elif not active and self.phase is Phase.IDLE:
            self.phase, self._progress = Phase.OUTRO, 0.0

    def set_band(self, width: int, height: int) -> None:
        """The free space under the tree changed; no room stops everything at once."""
        if (width, height) == self._size:
            return
        self._size = (width, height)
        if not self._fits():
            self.phase, self._scene = None, None
            return
        if self._scene is not None:
            self._scene.resize(width, height)
        elif self._active:
            self._start()

    def _start(self) -> None:
        self._scene = self._scene_type(random.Random(self._seed))
        self._scene.resize(*self._size)
        self.phase, self._progress = Phase.INTRO, 0.0

    def tick(self, dt: float) -> list[Cell]:
        """Advance by ``dt`` seconds and return the in-bounds cells to draw."""
        scene, phase = self._scene, self.phase
        if scene is None or phase is None:
            return []
        if phase is Phase.IDLE:
            self._progress += dt
        else:
            span = scene.intro_seconds if phase is Phase.INTRO else scene.outro_seconds
            self._progress = min(1.0, self._progress + dt / min(span, MAX_TRANSITION_SECONDS))
        cells = scene.frame(phase, self._progress, dt)
        if phase is Phase.INTRO and self._progress >= 1.0:
            # Left mid-intro: it still finishes, then goes straight into its outro.
            self.phase, self._progress = (Phase.IDLE if self._active else Phase.OUTRO), 0.0
        elif phase is Phase.OUTRO and self._progress >= 1.0:
            self.phase, self._scene = None, None
            if self._active and self._fits():  # wanted back mid-outro: it plays again after
                self._start()
            return []
        width, height = self._size
        return [cell for cell in cells if 0 <= cell.x < width and 0 <= cell.y < height]


__all__ = ["AmbienceDriver"]
