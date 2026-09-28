"""Launch-local OpenCode 2.x plugin: session receipts, approval enforcement, and MCP catalog.

2.x loads configured plugins only as directories exporting a default `{id, setup}`
(core/src/config/plugin/source.ts, core/src/plugin/module.ts), so this renders a small package.
"""

from __future__ import annotations

import json
from pathlib import Path

from theater.harness.base import theater_binary

from .approval_v2 import approval_ruleset
from .constants import (
    APPROVAL_EXEMPT_ACTIONS_V2,
    MCP_CATALOG_MAX_BYTES,
    MCP_CATALOG_MAX_SERVERS,
    MCP_CATALOG_MAX_TOOLS,
    MCP_CATALOG_NAME_MAX_BYTES,
    MCP_CATALOG_VERSION,
    RECEIPT_RETRY_DELAYS_MS,
)
from .mcp import catalog_path

PLUGIN_ID = "theater.opencode-session"
_PACKAGE = {"name": "theater-opencode-session", "private": True, "type": "module"}
_SETTINGS_SENTINEL = "__THEATER_SETTINGS__"

# Plain template: the body is JavaScript, so one sentinel keeps every brace literal.
_TEMPLATE = r"""import { spawn } from "node:child_process"
import { mkdir, rename, unlink, writeFile } from "node:fs/promises"
import { dirname } from "node:path"

const settings = __THEATER_SETTINGS__
const exempt = new Set(settings.exemptActions)
const sleep = (delay) => new Promise((resolve) => setTimeout(resolve, delay))

// Mirrors core/src/util/wildcard.ts so rules match exactly as native ones do.
function wildcard(pattern, value) {
  let source = pattern
    .replaceAll("\\", "/")
    .replace(/[.+^${}()|[\]\\]/g, "\\$&")
    .replace(/\*/g, ".*")
    .replace(/\?/g, ".")
  if (source.endsWith(" .*")) source = source.slice(0, -3) + "( .*)?"
  return new RegExp("^" + source + "$", "s").test(String(value).replaceAll("\\", "/"))
}

function approvalEffect(action, resources) {
  const targets = resources.length ? resources : ["*"]
  const effects = targets.map((resource) => {
    let effect = "ask"
    for (const rule of settings.rules) {
      if (wildcard(rule.action, action) && wildcard(rule.resource, resource)) effect = rule.effect
    }
    return effect
  })
  return effects.includes("ask") ? "ask" : "allow"
}

function publish(sessionID) {
  return new Promise((resolve) => {
    let settled = false
    const finish = (ok) => {
      if (settled) return
      settled = true
      resolve(ok)
    }
    try {
      const child = spawn(
        settings.theater,
        [
          "transcript-receipt",
          "--strict-exit",
          "--id",
          settings.participantID,
          "--token-file",
          settings.tokenPath,
        ],
        { stdio: ["pipe", "ignore", "ignore"] },
      )
      child.once("error", () => finish(false))
      child.once("close", (code) => finish(code === 0))
      child.stdin.once("error", () => finish(false))
      child.stdin.end(JSON.stringify({ session_id: sessionID }))
    } catch {
      finish(false)
    }
  })
}

let target = null
let delivered = null
let flight = null
let generation = 0

function report(sessionID) {
  if (typeof sessionID !== "string" || !sessionID) return
  if (sessionID === delivered || (sessionID === target && flight)) return
  target = sessionID
  const version = ++generation
  flight = (async () => {
    for (const delay of settings.retryDelays) {
      if (version !== generation) return
      if (delay > 0) await sleep(delay)
      if (version !== generation) return
      if (await publish(sessionID)) {
        if (version === generation) delivered = sessionID
        return
      }
    }
  })().finally(() => {
    if (version === generation) flight = null
  })
}

let serverNames = []
const mcpTools = new Map()
let catalogWrite = Promise.resolve()
let writeGeneration = 0

function boundedName(value) {
  return (
    typeof value === "string" &&
    value.trim() !== "" &&
    !/[\u0000-\u001f\u007f-\u009f]/u.test(value) &&
    Buffer.byteLength(value) <= settings.catalog.nameMaxBytes
  )
}

function identifyMcpTool(tool) {
  const matches = []
  for (const server of serverNames) {
    const prefix = `${server.replace(/[^a-zA-Z0-9_-]/g, "_")}_`
    if (tool.startsWith(prefix) && boundedName(tool.slice(prefix.length))) {
      matches.push([server, tool.slice(prefix.length)])
    }
  }
  return matches.length === 1 ? matches[0] : null
}

async function writeCatalog() {
  const catalog = () =>
    JSON.stringify({
      version: settings.catalog.version,
      servers: serverNames,
      tools: Object.fromEntries(mcpTools),
    })
  let encoded = catalog()
  while (Buffer.byteLength(encoded) > settings.catalog.maxBytes && mcpTools.size) {
    mcpTools.delete(mcpTools.keys().next().value)
    encoded = catalog()
  }
  if (Buffer.byteLength(encoded) > settings.catalog.maxBytes) return
  const temporary = `${settings.catalog.path}.${process.pid}.${++writeGeneration}.tmp`
  try {
    await mkdir(dirname(settings.catalog.path), { recursive: true, mode: 0o700 })
    await writeFile(temporary, encoded, { mode: 0o600 })
    await rename(temporary, settings.catalog.path)
  } catch {
    try {
      await unlink(temporary)
    } catch {}
  }
}

function persistCatalog() {
  catalogWrite = catalogWrite.then(writeCatalog, writeCatalog)
  return catalogWrite
}

async function refreshServers(ctx) {
  try {
    const result = await ctx.mcp.list()
    const listed = Array.isArray(result) ? result : result?.data
    const names = (Array.isArray(listed) ? listed : []).map((server) => server?.name)
    const found = [...new Set(names.filter(boundedName))].sort()
    if (found.length > settings.catalog.maxServers) return
    if (found.join("\u0000") === serverNames.join("\u0000")) return
    serverNames = found
    await persistCatalog()
  } catch {}
}

async function classify(ctx, tool) {
  if (!boundedName(tool) || mcpTools.has(tool) || mcpTools.size >= settings.catalog.maxTools) return
  let identity = identifyMcpTool(tool)
  if (!identity) {
    await refreshServers(ctx)
    identity = identifyMcpTool(tool)
  }
  if (!identity) return
  mcpTools.set(tool, identity)
  await persistCatalog()
}

async function register(label, attach) {
  try {
    return await attach()
  } catch (error) {
    console.error(`theater: ${label} is unavailable: ${error?.message ?? error}`)
    return null
  }
}

export default {
  id: settings.pluginID,
  async setup(ctx) {
    // Unguarded: if approval cannot be enforced the plugin fails, and no receipt ever vouches
    // for the session. The hook only tightens native allows (saved ones included) to asks.
    if (settings.rules.length) {
      await ctx.permission.hook("evaluate", (event) => {
        if (exempt.has(event.action) || event.effect !== "allow") return
        try {
          if (approvalEffect(event.action, Array.from(event.resources ?? [])) === "ask") {
            event.effect = "ask"
          }
        } catch {
          event.effect = "ask"
        }
      })
    }
    const abort = new AbortController()
    const directory = ctx.location?.directory
    await register("the prompt receipt", () =>
      ctx.session.hook("prompt", async (event) => {
        try {
          const info = await ctx.session.get({ sessionID: event.sessionID })
          if (info && !info.parentID) report(info.id ?? event.sessionID)
        } catch {}
      }),
    )
    await register("the MCP catalog", () =>
      ctx.tool.hook("execute.before", async (event) => {
        try {
          await classify(ctx, event.tool)
        } catch {}
      }),
    )
    void refreshServers(ctx)
    void (async () => {
      while (!abort.signal.aborted) {
        try {
          for await (const event of ctx.event.subscribe({ signal: abort.signal })) {
            const data = event?.data ?? {}
            const where = event?.location?.directory ?? data.location?.directory
            if (directory && where && where !== directory) continue
            if (event.type === "session.created" && !data.parentID) report(data.sessionID)
            else if (event.type === "session.forked") report(data.sessionID)
            else if (event.type === "mcp.status.changed") void refreshServers(ctx)
          }
        } catch {}
        if (!abort.signal.aborted) await sleep(1000)
      }
    })()
    return async () => {
      abort.abort()
      await catalogWrite
    }
  },
}
"""


def plugin_dir(config_path: Path) -> Path:
    return config_path.with_suffix(".opencode")


def render_native_plugin_v2(
    participant_id: str, config_path: Path, token_path: Path, approval: str
) -> dict[Path, str]:
    """The plugin package's files; yolo renders no rules and so registers no approval hook."""
    settings = {
        "pluginID": PLUGIN_ID,
        "participantID": participant_id,
        "tokenPath": str(token_path),
        "theater": theater_binary(),
        "retryDelays": list(RECEIPT_RETRY_DELAYS_MS),
        "rules": [dict(rule) for rule in approval_ruleset(approval)],
        "exemptActions": list(APPROVAL_EXEMPT_ACTIONS_V2),
        "catalog": {
            "path": str(catalog_path(participant_id)),
            "version": MCP_CATALOG_VERSION,
            "maxBytes": MCP_CATALOG_MAX_BYTES,
            "maxServers": MCP_CATALOG_MAX_SERVERS,
            "maxTools": MCP_CATALOG_MAX_TOOLS,
            "nameMaxBytes": MCP_CATALOG_NAME_MAX_BYTES,
        },
    }
    directory = plugin_dir(config_path)
    return {
        directory / "package.json": json.dumps(_PACKAGE, indent=2),
        directory / "server.js": _TEMPLATE.replace(_SETTINGS_SENTINEL, json.dumps(settings)),
    }


__all__ = ["PLUGIN_ID", "plugin_dir", "render_native_plugin_v2"]
