"""A quiet line on the band's bottom row that says the tree is here; the default."""

from __future__ import annotations

from regie.ambience.api import Cell, Phase, Scene


class FooterScene(Scene):
    """A thin rule that grows in from the centre, breathes rarely, and shrinks away."""

    name = "footer"
    fps = 4.0
    min_rows = 1
    intro_seconds = 1.2
    outro_seconds = 1.0

    _QUIET = ("-", "$secondary dim")
    _BREATHE = ("=", "$accent dim")
    _BREATHE_EVERY = 5  # idle seconds; the last of each cycle is the breathe

    def frame(self, phase: Phase, progress: float, dt: float) -> list[Cell]:
        if self.height < self.min_rows:
            return []
        glyph, style = self._QUIET
        if phase is Phase.IDLE:
            span = self.width
            if int(progress) % self._BREATHE_EVERY == self._BREATHE_EVERY - 1:
                glyph, style = self._BREATHE
        else:
            if phase is Phase.OUTRO and progress >= 1.0:
                return []
            share = progress if phase is Phase.INTRO else 1.0 - progress
            span = min(self.width, int(self.width * min(share, 1.0) + 0.5))
        if span <= 0:
            return []
        start = (self.width - span) // 2
        return [Cell(x, self.height - 1, glyph, style) for x in range(start, start + span)]


__all__ = ["FooterScene"]
