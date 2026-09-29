"""Recover OpenCode 2.0.18 frames after OpenTUI leaves a failed viewport clipped."""

from __future__ import annotations

import json

_TEMPLATE = """export default {
  id: __PLUGIN_ID__,
  setup({ app, renderer }) {
    if (app.version !== "2.0.18") return
    const root = renderer.root
    const original = root.render
    function render(buffer, deltaTime) {
      try {
        return original.call(this, buffer, deltaTime)
      } catch (error) {
        // OpenTUI skips the viewport's pop commands when a diff render throws.
        buffer.clearScissorRects()
        buffer.clearOpacity()
        renderer.clearHitGridScissorRects()
        renderer.requestRender()
        throw error
      }
    }
    root.render = render
    return () => {
      if (root.render === render) root.render = original
    }
  },
}
"""


def render_tui_recovery_v2(plugin_id: str) -> str:
    return _TEMPLATE.replace("__PLUGIN_ID__", json.dumps(plugin_id))
