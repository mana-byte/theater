"""Launch-local passive OpenCode TUI extension rendering."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

from theater import paths
from theater.harness.contracts.runtime import RuntimeFrontendInstallContext, RuntimeFrontendOverlay
from theater.models import BadRequest

_TUI_CONFIG = "theater-tui.json"
_TUI_PLUGIN = "theater-observer.mjs"


def install_opencode_tui_extension(
    context: RuntimeFrontendInstallContext,
) -> RuntimeFrontendOverlay:
    """Add a passive plugin without changing OpenCode's normal launch plan."""
    if os.environ.get("OPENCODE_TUI_CONFIG"):
        raise BadRequest("cannot compose the passive OpenCode extension over OPENCODE_TUI_CONFIG")
    parsed = urlparse(context.endpoint)
    if parsed.scheme != "unix" or not parsed.path:
        raise BadRequest("OpenCode passive extension requires a unix listener endpoint")
    root = paths.participant_observation_dir(context.participant_id, "opencode")
    config_path = root / _TUI_CONFIG
    plugin_path = root / _TUI_PLUGIN
    config = json.dumps({"plugin": [plugin_path.resolve().as_uri()]}, indent=2)
    return RuntimeFrontendOverlay(
        env={"OPENCODE_TUI_CONFIG": str(config_path)},
        files={
            config_path: config,
            plugin_path: render_opencode_tui_plugin(parsed.path, context.token_file),
        },
    )


def render_opencode_tui_plugin(socket_path: str, token_file: Path) -> str:
    path = json.dumps(socket_path)
    token = json.dumps(str(token_file))
    return f"""import net from "node:net"
import {{ readFile }} from "node:fs/promises"

const socketPath = {path}
const tokenPath = {token}
const maxLineBytes = 65536
const maxBufferedBytes = maxLineBytes * 8
const retryMaxMs = 5000
const snapshotIntervalMs = 1000
const tokenReadTimeoutMs = 1500

function within(promise, timeout) {{
  return new Promise((resolve, reject) => {{
    let settled = false
    const deadline = setTimeout(() => {{
      if (settled) return
      settled = true
      reject(new Error("OpenCode observer token read timed out"))
    }}, timeout)
    Promise.resolve(promise).then(
      (value) => {{
        if (settled) return
        settled = true
        clearTimeout(deadline)
        resolve(value)
      }},
      (error) => {{
        if (settled) return
        settled = true
        clearTimeout(deadline)
        reject(error)
      }},
    )
  }})
}}

function compact(value, depth = 0, seen = new WeakSet()) {{
  if (value === null || typeof value === "boolean" || typeof value === "number") return value
  if (typeof value === "string") return value.length > 4096 ? value.slice(0, 4096) : value
  if (depth >= 5 || typeof value !== "object") return undefined
  if (seen.has(value)) return undefined
  seen.add(value)
  if (Array.isArray(value)) return value.slice(0, 32).map((item) => compact(item, depth + 1, seen))
  const out = {{}}
  for (const [key, item] of Object.entries(value).slice(0, 48)) {{
    const next = compact(item, depth + 1, seen)
    if (next !== undefined) out[key] = next
  }}
  return out
}}

const tui = async (api) => {{
  let stopped = false
  let socket = null
  let socketReady = false
  let timer = null
  let snapshotTimer = null
  let retryMs = 100
  let connecting = false
  let snapshotWanted = false
  let snapshotRunning = false
  let routeSession = null
  let routeEpoch = 0

  const routeState = () => {{
    const route = api.route.current
    const id = route?.name === "session" && typeof route.params?.sessionID === "string"
      ? route.params.sessionID
      : null
    if (id !== routeSession) {{
      routeSession = id
      routeEpoch += 1
    }}
    return {{ id, epoch: routeEpoch, route }}
  }}

  const send = (type, payload, expected = socket) => {{
    if (!socketReady || !expected || expected !== socket || expected.destroyed) return
    let line
    try {{
      line = JSON.stringify({{ type, ...payload }}) + "\\n"
    }} catch {{
      return
    }}
    const bytes = Buffer.byteLength(line)
    if (bytes > maxLineBytes) return
    if (expected.writableLength + bytes > maxBufferedBytes) {{
      expected.destroy()
      return
    }}
    try {{
      expected.write(line)
    }} catch {{
      expected.destroy()
    }}
  }}

  const snapshot = async () => {{
    const expected = socket
    const current = routeState()
    if (!socketReady || !expected) return
    if (!current.id) {{
      send("snapshot", {{ session_id: null, route_session_id: null,
        session_epoch: current.epoch }}, expected)
      return
    }}
    try {{
      const status = api.state.session.status(current.id)
      send("snapshot", {{
        session_id: current.id,
        route_session_id: current.id,
        session_epoch: current.epoch,
        route: compact(current.route),
        status: compact(status),
        message_count: api.state.session.messages(current.id).length,
        permission_count: api.state.session.permission(current.id).length,
        question_count: api.state.session.question(current.id).length,
      }}, expected)
    }} catch {{}}
  }}

  const requestSnapshot = () => {{
    snapshotWanted = true
    if (snapshotRunning) return
    snapshotRunning = true
    queueMicrotask(() => void flushSnapshots())
  }}

  const flushSnapshots = async () => {{
    try {{
      while (!stopped && snapshotWanted) {{
        snapshotWanted = false
        await snapshot()
      }}
    }} finally {{
      snapshotRunning = false
      if (!stopped && snapshotWanted) requestSnapshot()
    }}
  }}

  const schedule = () => {{
    if (stopped || timer) return
    const delay = retryMs
    retryMs = Math.min(retryMs * 2, retryMaxMs)
    timer = setTimeout(() => {{
      timer = null
      void connect()
    }}, delay)
  }}

  const connect = async () => {{
    if (stopped || socket || connecting) return
    connecting = true
    try {{
      const token = (await within(readFile(tokenPath, "utf8"), tokenReadTimeoutMs)).trim()
      if (stopped || socket || !token) {{
        if (!stopped && !socket) schedule()
        return
      }}
      const candidate = net.createConnection(socketPath)
      socket = candidate
      let settled = false
      const disconnect = () => {{
        if (settled) return
        settled = true
        if (socket === candidate) {{
          socket = null
          socketReady = false
        }}
        if (!stopped) schedule()
      }}
      candidate.once("connect", () => {{
        if (stopped || socket !== candidate) {{
          candidate.destroy()
          return
        }}
        retryMs = 100
        socketReady = true
        send("hello", {{ protocol: "theater-frontend-v1", token }}, candidate)
        requestSnapshot()
      }})
      candidate.once("error", disconnect)
      candidate.once("close", disconnect)
    }} catch {{
      if (!stopped && !socket) schedule()
    }} finally {{
      connecting = false
    }}
  }}

  api.event.on("session.status", (event) => {{
    if (stopped) return
    const current = routeState()
    const sessionID = event?.properties?.sessionID
    if (current.id && sessionID === current.id) {{
      send("event", {{
        session_id: sessionID,
        route_session_id: current.id,
        session_epoch: current.epoch,
        event: compact(event),
      }})
    }}
    requestSnapshot()
  }})
  api.event.on("tui.session.select", () => {{
    if (!stopped) requestSnapshot()
  }})
  snapshotTimer = setInterval(requestSnapshot, snapshotIntervalMs)
  api.lifecycle.onDispose(() => {{
    stopped = true
    if (timer) clearTimeout(timer)
    timer = null
    if (snapshotTimer) clearInterval(snapshotTimer)
    snapshotTimer = null
    socket?.destroy()
    socket = null
    socketReady = false
  }})
  await connect()
}}

export default {{
  id: "theater.opencode.observer",
  tui,
}}
"""


__all__ = ["install_opencode_tui_extension", "render_opencode_tui_plugin"]
