"""Execute the generated TUI entry point across a failed and recovered frame."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from theater.harness.builtin.plugins.opencode.native_plugin_v2 import (
    plugin_dir,
    render_native_plugin_v2,
)

_PROBE = """
import assert from 'node:assert/strict'
const { default: plugin } = await import(process.argv[1])
const failure = new RangeError('visual row count exceeds native u32 length limit')
let failed = false
let repaints = 0
let hitClip = false
const buffer = {
  clipped: false, opacity: 1,
  clearScissorRects() { this.clipped = false },
  clearOpacity() { this.opacity = 1 },
}
const root = {
  render(current, deltaTime) {
    assert.equal(this, root)
    assert.equal(current, buffer)
    assert.equal(deltaTime, 16)
    if (!failed) {
      failed = true
      current.clipped = true
      current.opacity = 0.5
      hitClip = true
      throw failure
    }
    assert.equal(current.clipped, false)
    assert.equal(current.opacity, 1)
    assert.equal(hitClip, false)
    return 'header, chat, sidebar, prompt'
  },
}
const renderer = {
  root,
  clearHitGridScissorRects() { hitClip = false },
  requestRender() { repaints++ },
}
const original = root.render
for (const version of ['2.0.16', '2.0.99']) {
  assert.equal(plugin.setup({ app: { version }, renderer }), undefined)
  assert.equal(root.render, original)
}
for (const version of ['2.0.17', '2.0.18', '2.0.19', '2.0.20']) {
  const dispose = plugin.setup({ app: { version }, renderer })
  assert.throws(() => root.render(buffer, 16), error => error === failure)
  assert.equal(repaints, 1)
  assert.equal(root.render(buffer, 16), 'header, chat, sidebar, prompt')
  assert.equal(repaints, 1)
  dispose()
  assert.equal(root.render, original)
  failed = false
  repaints = 0
  hitClip = false
}
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the plugin")
def test_generated_tui_recovers_a_failed_viewport(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    for path, content in render_native_plugin_v2(
        "test", config, tmp_path / "token", "manual"
    ).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(
        ["node", "--input-type=module", "-e", _PROBE, (plugin_dir(config) / "tui.js").as_uri()],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
