"""Launch-local OpenCode TUI extension rendering.

The plugin is an official in-TUI extension: it observes public TUI state and
performs only the daemon-requested ``opencode.send`` mutation through the
public SDK client. It never touches OpenCode internals or the stock UI.
"""

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
_SOCKET_SENTINEL = "__THEATER_FRONTEND_SOCKET__"
_TOKEN_SENTINEL = "__THEATER_FRONTEND_TOKEN__"

# Plain template: the plugin body is JavaScript, so sentinel replacement keeps
# every brace literal instead of f-string escaping the whole file.
_TUI_PLUGIN_TEMPLATE = """import net from "node:net"
import { readFile } from "node:fs/promises"
import { randomBytes } from "node:crypto"

const socketPath = __THEATER_FRONTEND_SOCKET__
const tokenPath = __THEATER_FRONTEND_TOKEN__
const maxLineBytes = 65536
const maxInboundLineBytes = 65536
const maxBufferedBytes = maxLineBytes * 8
const retryMaxMs = 5000
const snapshotIntervalMs = 1000
const tokenReadTimeoutMs = 1500
const mutationTimeoutMs = 15000
const maxOperations = 64
const maxIdChars = 512
const maxPromptChars = 60000

function within(promise, timeout) {
  return new Promise((resolve, reject) => {
    let settled = false
    const deadline = setTimeout(() => {
      if (settled) return
      settled = true
      reject(new Error("OpenCode observer token read timed out"))
    }, timeout)
    Promise.resolve(promise).then(
      (value) => {
        if (settled) return
        settled = true
        clearTimeout(deadline)
        resolve(value)
      },
      (error) => {
        if (settled) return
        settled = true
        clearTimeout(deadline)
        reject(error)
      },
    )
  })
}

function compact(value, depth = 0, seen = new WeakSet()) {
  if (value === null || typeof value === "boolean" || typeof value === "number") return value
  if (typeof value === "string") return value.length > 4096 ? value.slice(0, 4096) : value
  if (depth >= 5 || typeof value !== "object") return undefined
  if (seen.has(value)) return undefined
  seen.add(value)
  if (Array.isArray(value)) return value.slice(0, 32).map((item) => compact(item, depth + 1, seen))
  const out = {}
  for (const [key, item] of Object.entries(value).slice(0, 48)) {
    const next = compact(item, depth + 1, seen)
    if (next !== undefined) out[key] = next
  }
  return out
}

// OpenCode's public messageID schema requires the "msg" prefix
// (packages/opencode/src/session/schema.ts MessageID). The shape mirrors the
// upstream generator without importing an unexported source module.
const base62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
let messageTime = 0
let messageCounter = 0

function makeMessageID() {
  const now = Date.now()
  if (now !== messageTime) {
    messageTime = now
    messageCounter = 0
  }
  messageCounter += 1
  const value = BigInt(now) * 0x1000n + BigInt(messageCounter % 0x1000)
  const timeBytes = Buffer.alloc(6)
  for (let i = 0; i < 6; i += 1) timeBytes[i] = Number((value >> BigInt(40 - 8 * i)) & 0xffn)
  const bytes = randomBytes(14)
  let random = ""
  for (let i = 0; i < 14; i += 1) random += base62[bytes[i] % 62]
  return "msg_" + timeBytes.toString("hex") + random
}

function errorReply(code, message) {
  return { error: { code, message: String(message).slice(0, 512) } }
}

function acceptedReply(params, sessionID, messageID, epoch) {
  return {
    result: {
      status: "accepted",
      operation_id: params.operation_id,
      native_session_id: sessionID,
      native_turn_id: messageID,
      session_epoch: epoch,
    },
  }
}

const tui = async (api) => {
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
  // Once-only receipts scoped to the current route epoch: a switch or
  // reconnect invalidates every cached fact, and only the oldest SETTLED
  // receipt evicts — in-flight work is never dropped, so long-lived
  // sessions keep sending (Theater's durable store stops old replays).
  const operations = new Map()
  const inFlight = new Set()
  let mutationTail = Promise.resolve()
  const settle = (operationId, reply) => {
    operations.set(operationId, { reply })
    if (operations.size > maxOperations) {
      operations.delete(operations.keys().next().value)
    }
  }


  const routeState = () => {
    const route = api.route.current
    const id = route?.name === "session" && typeof route.params?.sessionID === "string"
      ? route.params.sessionID
      : null
    if (id !== routeSession) {
      routeSession = id
      routeEpoch += 1
      operations.clear()
    }
    return { id, epoch: routeEpoch, route }
  }

  const send = (type, payload, expected = socket) => {
    if (!socketReady || !expected || expected !== socket || expected.destroyed) return
    let line
    try {
      line = JSON.stringify({ type, ...payload }) + "\\n"
    } catch {
      return
    }
    const bytes = Buffer.byteLength(line)
    if (bytes > maxLineBytes) return
    if (expected.writableLength + bytes > maxBufferedBytes) {
      expected.destroy()
      return
    }
    try {
      expected.write(line)
    } catch {
      expected.destroy()
    }
  }

  const respond = (id, reply, expected) => {
    if (!expected || expected.destroyed) return
    send("response", { id, ...reply }, expected)
  }

  const validateSendParams = (params) => {
    if (params === null || typeof params !== "object" || Array.isArray(params)) {
      return errorReply("invalid_request", "opencode.send params must be an object")
    }
    const boundedString = (value) =>
      typeof value === "string" && value.length > 0 && value.length <= maxIdChars
    if (!boundedString(params.operation_id)) {
      return errorReply("invalid_request", "operation_id must be a bounded non-blank string")
    }
    if (!boundedString(params.native_session_id)) {
      return errorReply("invalid_request", "native_session_id must be a bounded non-blank string")
    }
    if (typeof params.prompt !== "string" || !params.prompt
      || params.prompt.length > maxPromptChars) {
      return errorReply("invalid_request", "prompt must be a bounded non-blank string")
    }
    return null
  }

  // The prompt may have crossed the SDK boundary whenever the call throws,
  // resolves without a definite HTTP response, or the visible session moves:
  // those replies are unknown to Theater and are never retried here.
  const performSend = async (params) => {
    const current = routeState()
    if (!current.id) {
      return errorReply("not_ready", "the stock TUI is not on a session route")
    }
    if (current.id !== params.native_session_id) {
      return errorReply(
        "wrong_session",
        `the visible session ${current.id} is not the requested session`,
      )
    }
    let status
    try {
      status = api.state.session.status(current.id)
    } catch {
      status = undefined
    }
    if (status === null || typeof status !== "object" || typeof status.type !== "string") {
      return errorReply("not_ready", "the visible session has no current status")
    }
    if (status.type !== "idle") {
      return errorReply("busy", `the OpenCode session is not idle (${status.type})`)
    }
    const messageID = makeMessageID()
    let result
    // The TUI types api.client as the v2 SDK client (packages/plugin/src/tui.ts,
    // @opencode-ai/sdk/v2): promptAsync takes a flat {sessionID, messageID, parts}.
    result = await api.client.session.promptAsync({
      sessionID: current.id,
      messageID,
      parts: [{ type: "text", text: params.prompt }],
    })
    if (result === null || typeof result !== "object") {
      throw new Error("promptAsync returned a malformed result")
    }
    if (result.error !== undefined) {
      if (result.response && typeof result.response.status === "number") {
        return errorReply(
          "delivery_unknown",
          `OpenCode answered the prompt with HTTP ${result.response.status}`,
        )
      }
      throw new Error("promptAsync failed without a definite response")
    }
    if (!result.response || result.response.status !== 204) {
      throw new Error("promptAsync returned an unexpected status")
    }
    const after = routeState()
    if (after.id !== current.id || after.epoch !== current.epoch) {
      throw new Error("the visible session changed while the prompt was in flight")
    }
    return acceptedReply(params, current.id, messageID, current.epoch)
  }

  const withMutationTimeout = (promise) => Promise.race([
    promise,
    new Promise((resolve, reject) => {
      setTimeout(() => reject(new Error("opencode.send mutation timed out")), mutationTimeoutMs)
    }),
  ])

  const handleSend = (target, frame) => {
    const failure = validateSendParams(frame.params)
    if (failure) {
      respond(frame.id, failure, target)
      return
    }
    const operationId = frame.params.operation_id
    if (inFlight.has(operationId)) {
      respond(
        frame.id,
        errorReply("operation_in_progress", "the operation is still executing"),
        target,
      )
      return
    }
    const previous = operations.get(operationId)
    if (previous) {
      respond(frame.id, previous.reply, target)
      return
    }
    inFlight.add(operationId)
    // Mutations serialize on one tail so a later request can never validate
    // the route in the gap opened by an earlier SDK await.
    mutationTail = mutationTail
      .then(() => withMutationTimeout(performSend(frame.params)))
      .then(
        (reply) => {
          inFlight.delete(operationId)
          settle(operationId, reply)
          respond(frame.id, reply, target)
        },
        () => {
          inFlight.delete(operationId)
          const reply = errorReply(
            "delivery_unknown",
            "the OpenCode prompt delivery became uncertain",
          )
          settle(operationId, reply)
          respond(frame.id, reply, target)
        },
      )
  }

  const handleRequest = (target, frame) => {
    if (frame.method !== "opencode.send") {
      respond(
        frame.id,
        errorReply("invalid_request", `unsupported method ${String(frame.method)}`),
        target,
      )
      return
    }
    handleSend(target, frame)
  }

  let inbound = Buffer.alloc(0)

  const handleData = (chunk, target) => {
    inbound = Buffer.concat([inbound, chunk])
    for (;;) {
      const newline = inbound.indexOf(10)
      if (newline < 0) {
        if (inbound.length > maxInboundLineBytes) target.destroy()
        return
      }
      const line = inbound.subarray(0, newline)
      inbound = inbound.subarray(newline + 1)
      if (line.length === 0 || line.length > maxInboundLineBytes) {
        target.destroy()
        return
      }
      let frame = null
      try {
        frame = JSON.parse(line.toString("utf8"))
      } catch {
        target.destroy()
        return
      }
      if (
        frame === null
        || typeof frame !== "object"
        || frame.type !== "request"
        || typeof frame.id !== "string"
        || !frame.id
        || frame.id.length > maxIdChars
      ) {
        target.destroy()
        return
      }
      handleRequest(target, frame)
    }
  }

  const snapshot = async () => {
    const expected = socket
    const current = routeState()
    if (!socketReady || !expected) return
    if (!current.id) {
      send("snapshot", { session_id: null, route_session_id: null,
        session_epoch: current.epoch }, expected)
      return
    }
    try {
      const status = api.state.session.status(current.id)
      send("snapshot", {
        session_id: current.id,
        route_session_id: current.id,
        session_epoch: current.epoch,
        route: compact(current.route),
        status: compact(status),
        message_count: api.state.session.messages(current.id).length,
        permission_count: api.state.session.permission(current.id).length,
        question_count: api.state.session.question(current.id).length,
      }, expected)
    } catch {}
  }

  const requestSnapshot = () => {
    snapshotWanted = true
    if (snapshotRunning) return
    snapshotRunning = true
    queueMicrotask(() => void flushSnapshots())
  }

  const flushSnapshots = async () => {
    try {
      while (!stopped && snapshotWanted) {
        snapshotWanted = false
        await snapshot()
      }
    } finally {
      snapshotRunning = false
      if (!stopped && snapshotWanted) requestSnapshot()
    }
  }

  const schedule = () => {
    if (stopped || timer) return
    const delay = retryMs
    retryMs = Math.min(retryMs * 2, retryMaxMs)
    timer = setTimeout(() => {
      timer = null
      void connect()
    }, delay)
  }

  const resetConnectionState = (candidate) => {
    if (socket === candidate) {
      socket = null
      socketReady = false
    }
    inbound = Buffer.alloc(0)
    operations.clear()
    inFlight.clear()
  }

  const connect = async () => {
    if (stopped || socket || connecting) return
    connecting = true
    try {
      const token = (await within(readFile(tokenPath, "utf8"), tokenReadTimeoutMs)).trim()
      if (stopped || socket || !token) {
        if (!stopped && !socket) schedule()
        return
      }
      const candidate = net.createConnection(socketPath)
      socket = candidate
      let settled = false
      const disconnect = () => {
        if (settled) return
        settled = true
        resetConnectionState(candidate)
        if (!stopped) schedule()
      }
      candidate.once("connect", () => {
        if (stopped || socket !== candidate) {
          candidate.destroy()
          return
        }
        retryMs = 100
        socketReady = true
        routeState()
        send("hello", { protocol: "theater-frontend-v1", token }, candidate)
        requestSnapshot()
      })
      candidate.on("data", (chunk) => handleData(chunk, candidate))
      candidate.once("error", disconnect)
      candidate.once("close", disconnect)
    } catch {
      if (!stopped && !socket) schedule()
    } finally {
      connecting = false
    }
  }

  api.event.on("session.status", (event) => {
    if (stopped) return
    const current = routeState()
    const sessionID = event?.properties?.sessionID
    if (current.id && sessionID === current.id) {
      send("event", {
        session_id: sessionID,
        route_session_id: current.id,
        session_epoch: current.epoch,
        event: compact(event),
      })
    }
    requestSnapshot()
  })
  api.event.on("message.updated", (event) => {
    if (stopped) return
    const current = routeState()
    const sessionID = event?.properties?.sessionID
    const info = event?.properties?.info
    if (current.id && sessionID === current.id && info?.role === "assistant") {
      send("event", {
        session_id: sessionID,
        route_session_id: current.id,
        session_epoch: current.epoch,
        event: compact(event),
      })
    }
  })
  api.event.on("tui.session.select", () => {
    if (!stopped) requestSnapshot()
  })
  snapshotTimer = setInterval(requestSnapshot, snapshotIntervalMs)
  api.lifecycle.onDispose(() => {
    stopped = true
    if (timer) clearTimeout(timer)
    timer = null
    if (snapshotTimer) clearInterval(snapshotTimer)
    snapshotTimer = null
    socket?.destroy()
    socket = null
    socketReady = false
  })
  await connect()
}

export default {
  id: "theater.opencode.observer",
  tui,
}
"""


def install_opencode_tui_extension(
    context: RuntimeFrontendInstallContext,
) -> RuntimeFrontendOverlay:
    """Add the stock-UI extension without changing OpenCode's launch plan."""
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
    return _TUI_PLUGIN_TEMPLATE.replace(_SOCKET_SENTINEL, json.dumps(socket_path)).replace(
        _TOKEN_SENTINEL, json.dumps(str(token_file))
    )


__all__ = ["install_opencode_tui_extension", "render_opencode_tui_plugin"]
