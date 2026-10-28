"""The OpenCode 2.x plugin: load gate, approval ruleset, tightened decisions and TUI recovery."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.support.opencode_server import PASSWORD, ServerFake
from theater.harness.builtin.plugins.opencode.approval import (
    PluginNotActive,
    approval_ruleset,
    classify_plugin_state,
    require_plugin_active,
)
from theater.harness.builtin.plugins.opencode.native_plugin import (
    PLUGIN_ID,
    plugin_dir,
    render_native_plugin,
)
from theater.harness.builtin.plugins.opencode.session_http import OpenCodeSessionClient
from theater.models import BadRequest

# The fake records every request, so tests assert what was polled and what refused.

SOURCE = "/work/x.opencode/server.js"


class PluginFake(ServerFake):
    """`/api/plugin` with a scripted entry list; records every request."""

    def __init__(self) -> None:
        super().__init__()
        self.plugins: list[dict[str, Any]] = []
        self.plugin_after = 0

    async def _route(self, record: dict[str, Any], writer, body: bytes) -> None:
        if not self._authorized(record):
            await self._send(writer, 401, "Unauthorized", b"{}")
            return
        if [s for s in record["path"].split("/") if s] == ["api", "plugin"]:
            seen = len([r for r in self.requests if r["path"].startswith("/api/plugin")])
            if not self.plugins and self.plugin_after and seen > self.plugin_after:
                answer: object = {"data": [self._active_entry()]}
            else:
                answer = {"data": list(self.plugins)}
            await self._send(writer, 200, "OK", json.dumps(answer).encode())
            return
        await self._send(writer, 404, "Not Found", b"{}")

    def _active_entry(self) -> dict[str, Any]:
        return active_entry()

    def plugin_requests(self) -> int:
        return len([r for r in self.requests if r["path"].startswith("/api/plugin")])


def active_entry(**overrides: object) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": PLUGIN_ID,
        "source": {"type": "local", "path": SOURCE},
        "state": {"status": "active"},
    }
    entry.update(overrides)
    return entry


@pytest.fixture
async def fake():
    server = PluginFake()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
def client(fake: PluginFake, tmp_path: Path) -> OpenCodeSessionClient:
    token = tmp_path / "credential"
    token.write_text(PASSWORD)
    token.chmod(0o600)
    return OpenCodeSessionClient(endpoint=fake.endpoint, token_file=token)


def test_a_missing_plugin_refuses():
    with pytest.raises(PluginNotActive, match="not loaded"):
        classify_plugin_state([], plugin_id=PLUGIN_ID, source_path=SOURCE)


@pytest.mark.parametrize("approval", ["", "automatic"])
def test_unknown_approval_cannot_render_an_unrestricted_plugin(tmp_path, approval):
    with pytest.raises(BadRequest, match="unknown approval"):
        render_native_plugin("test", tmp_path / "config", tmp_path / "token", approval)
    assert approval_ruleset("yolo") == ()


def test_a_failed_plugin_refuses_with_its_error():
    entry = active_entry(state={"status": "failed", "error": "boom"})
    with pytest.raises(PluginNotActive, match="boom"):
        classify_plugin_state([entry], plugin_id=PLUGIN_ID, source_path=SOURCE)


def test_an_ambiguous_plugin_refuses():
    with pytest.raises(PluginNotActive, match="2 times"):
        classify_plugin_state(
            [active_entry(), active_entry()], plugin_id=PLUGIN_ID, source_path=SOURCE
        )


def test_the_plugin_directory_is_the_generated_plugin():
    entry = active_entry(source={"type": "local", "path": str(Path(SOURCE).parent)})
    classify_plugin_state([entry], plugin_id=PLUGIN_ID, source_path=SOURCE)


def test_a_foreign_plugin_path_refuses():
    entry = active_entry(source={"type": "local", "path": "/elsewhere/server.js"})
    with pytest.raises(PluginNotActive, match="not the generated"):
        classify_plugin_state([entry], plugin_id=PLUGIN_ID, source_path=SOURCE)


def test_a_nonlocal_source_refuses():
    entry = active_entry(source={"type": "npm", "path": SOURCE})
    with pytest.raises(PluginNotActive, match="not the launch-local"):
        classify_plugin_state([entry], plugin_id=PLUGIN_ID, source_path=SOURCE)


async def test_a_missing_plugin_is_polled_until_it_registers(fake, client):
    fake.plugin_after = 2
    await require_plugin_active(client, plugin_id=PLUGIN_ID, source_path=SOURCE, interval=0.01)
    assert fake.plugin_requests() >= 3


async def test_a_reported_plugin_failure_refuses_immediately(fake, client):
    fake.plugins = [active_entry(state={"status": "failed", "error": "boom"})]
    with pytest.raises(PluginNotActive, match="boom"):
        await require_plugin_active(client, plugin_id=PLUGIN_ID, source_path=SOURCE)
    assert fake.plugin_requests() == 1


async def test_a_plugin_that_never_registers_refuses_at_the_deadline(fake, client):
    with pytest.raises(PluginNotActive, match="not loaded"):
        await require_plugin_active(
            client, plugin_id=PLUGIN_ID, source_path=SOURCE, timeout=0.05, interval=0.01
        )


_PLUGIN_PROBE = """
const plugin = (await import(process.argv[1])).default
const hooks = {}
const registered = (name) => async (hook, callback) => {
  hooks[`${name}.${hook}`] = callback
  return { dispose: async () => {} }
}
const never = { [Symbol.asyncIterator]: () => ({ next: () => new Promise(() => {}) }) }
await plugin.setup({
  location: { directory: "/w" },
  permission: { hook: registered("permission") },
  session: { hook: registered("session"), get: async () => ({ id: "ses_1" }) },
  tool: { hook: registered("tool") },
  mcp: { list: async () => ({ data: [] }) },
  event: { subscribe: () => never },
})
const decide = (action, resources, effect = "allow") => {
  const event = { action, resources, effect }
  hooks["permission.evaluate"]?.(event)
  return event.effect
}
console.log(JSON.stringify({
  id: plugin.id,
  shell: decide("shell", ["ls"]),
  read: decide("read", ["/w/a.txt"]),
  env: decide("read", ["/w/.env"]),
  example: decide("read", ["/w/.env.example"]),
  edit: decide("edit", ["/w/a.txt"]),
  model: decide("provider.use", ["anthropic/claude"]),
  nativeAsk: decide("read", ["/w/a.txt"], "ask"),
}))
process.exit(0)
"""


@pytest.mark.requires_node
@pytest.mark.parametrize(
    ("approval", "edit", "shell"),
    [("manual", "ask", "ask"), ("edits", "allow", "ask"), ("yolo", "allow", "allow")],
)
def test_the_plugin_only_tightens_native_decisions(tmp_path, approval, edit, shell):
    files = render_native_plugin("abc123", tmp_path / "x.json", tmp_path / "token", approval)
    for path, body in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    server = plugin_dir(tmp_path / "x.json") / "server.js"
    run = subprocess.run(
        ["node", "--input-type=module", "-e", _PLUGIN_PROBE, server.as_uri()],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    decided = json.loads(run.stdout)
    assert decided["id"] == "theater.opencode-session"
    assert (decided["edit"], decided["shell"]) == (edit, shell)
    assert decided["read"] == "allow" and decided["model"] == "allow"
    assert decided["nativeAsk"] == "ask"
    if approval != "yolo":
        assert (decided["env"], decided["example"]) == ("ask", "allow")


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
for (const version of ['2.0.17', '2.0.99']) {
  assert.equal(plugin.setup({ app: { version }, renderer }), undefined)
  assert.equal(root.render, original)
}
for (const version of ['2.0.18', '2.0.19', '2.0.20']) {
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


@pytest.mark.requires_node
def test_generated_tui_recovers_a_failed_viewport(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    for path, content in render_native_plugin("test", config, tmp_path / "token", "manual").items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(
        ["node", "--input-type=module", "-e", _PROBE, (plugin_dir(config) / "tui.js").as_uri()],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
