"""Régie animation behavior, organized by concern.

Import concrete submodules directly (``from theater.regie.animations.pulse
import working_harness_style``); this package is intentionally dependency-free
so importing any submodule never triggers the others and never re-enters
``render.glyphs`` mid-initialization.

- :mod:`pulse` — shared grayscale frame/style lookup for the working-harness wave
- :mod:`routes` — send/await route state, glyph mechanics, and controller
- :mod:`reveal` — keyed leaf-reveal controller and pure clipping
- :mod:`footer` — footer counter interpolation and pulsing Content
- :mod:`cycling_text` — cycling styled-text state machine for the dashboard

Modules are Textual-App/Widget independent; only ``footer`` imports Textual
``Content``. Widgets render/apply frames; controllers compute state.
"""

from __future__ import annotations

__all__ = [
    "cycling_text",
    "footer",
    "pulse",
    "reveal",
    "routes",
    "spinner",
]
