"""Launch-local OpenCode plugin generation."""

from __future__ import annotations

import json
from pathlib import Path

from theater.harness.base import theater_binary

from .constants import (
    MCP_CATALOG_MAX_BYTES,
    MCP_CATALOG_MAX_NON_MCP_TOOLS,
    MCP_CATALOG_MAX_SERVERS,
    MCP_CATALOG_MAX_TOOLS,
    MCP_CATALOG_NAME_MAX_BYTES,
    MCP_CATALOG_VERSION,
    RECEIPT_RETRY_DELAYS_MS,
)
from .mcp import catalog_path


def render_native_plugin(participant_id: str, token_path: Path, permission_rules=()) -> str:
    """Render the launch-local plugin.

    ``permission_rules`` is the approval choice's session ruleset
    (``_APPROVAL_SESSION_RULES[approval]``, native ``PermissionV1.Ruleset``
    array shape). The plugin appends it to each session's permission through
    the session update route — the one native layer merged after the agent's
    own rules — before the first LLM call. Existing effective denies are
    appended after these rules so the approval policy never weakens them.
    Empty (yolo) means the plugin enforces nothing and ``--auto`` runs
    unattended, as before.
    """
    participant = json.dumps(participant_id)
    token = json.dumps(str(token_path))
    command = json.dumps(theater_binary())
    retry_delays = json.dumps(RECEIPT_RETRY_DELAYS_MS)
    mcp_catalog = json.dumps(str(catalog_path(participant_id)))
    rules = json.dumps([dict(rule) for rule in permission_rules])
    return f"""import {{ spawn }} from "node:child_process"
import {{ mkdir, rename, unlink, writeFile }} from "node:fs/promises"
import {{ dirname }} from "node:path"

const participantID = {participant}
const tokenPath = {token}
const theater = {command}
const retryDelays = {retry_delays}
const catalogPath = {mcp_catalog}
const catalogVersion = {MCP_CATALOG_VERSION}
const catalogMaxBytes = {MCP_CATALOG_MAX_BYTES}
const catalogMaxServers = {MCP_CATALOG_MAX_SERVERS}
const catalogMaxTools = {MCP_CATALOG_MAX_TOOLS}
const catalogMaxDefinitions = {MCP_CATALOG_MAX_NON_MCP_TOOLS}
const catalogNameMaxBytes = {MCP_CATALOG_NAME_MAX_BYTES}
const permissionRules = {rules}
let currentSessionID = null
let deliveredSessionID = null
let publishing = false
let generation = 0
let writeGeneration = 0
let catalogWrite = Promise.resolve()
let serverRefresh = null
let serverNames = []
let serversComplete = true
let definitionsComplete = true
const enforcedSessions = new Set()
const enforcementFlights = new Map()
const nativeTools = new Set()
const mcpTools = new Map()
const unclassifiedTools = new Set()

function boundedName(value) {{
  return (
    typeof value === "string" &&
    value.trim() &&
    !/[\u0000-\u001f\u007f-\u009f]/u.test(value) &&
    Buffer.byteLength(value) <= catalogNameMaxBytes
  )
}}

function sanitize(value) {{
  return value.replace(/[^a-zA-Z0-9_-]/g, "_")
}}

function setServers(values) {{
  const observed = [...mcpTools.values()].map(([server]) => server)
  const found = [...new Set([...values, ...observed].filter(boundedName))].sort()
  const complete = found.length <= catalogMaxServers
  const next = complete ? found : [...new Set(observed)].sort()
  const changed =
    complete !== serversComplete ||
    next.length !== serverNames.length ||
    next.some((server, index) => server !== serverNames[index])
  serversComplete = complete
  serverNames = next
  if (changed) unclassifiedTools.clear()
  return changed
}}

function identifyMcpTool(tool) {{
  if (!serversComplete || !definitionsComplete || nativeTools.has(tool) || !boundedName(tool)) {{
    return null
  }}
  const matches = []
  for (const server of serverNames) {{
    const prefix = `${{sanitize(server)}}_`
    if (tool.startsWith(prefix) && boundedName(tool.slice(prefix.length))) {{
      matches.push([server, tool.slice(prefix.length)])
    }}
  }}
  return matches.length === 1 ? matches[0] : null
}}

async function writeCatalog() {{
  const tools = Object.fromEntries(mcpTools)
  let encoded = JSON.stringify({{ version: catalogVersion, servers: serverNames, tools }})
  while (Buffer.byteLength(encoded) > catalogMaxBytes && mcpTools.size) {{
    mcpTools.delete(mcpTools.keys().next().value)
    encoded = JSON.stringify({{
      version: catalogVersion,
      servers: serverNames,
      tools: Object.fromEntries(mcpTools),
    }})
  }}
  if (Buffer.byteLength(encoded) > catalogMaxBytes) return
  const temporary = `${{catalogPath}}.${{process.pid}}.${{++writeGeneration}}.tmp`
  try {{
    await mkdir(dirname(catalogPath), {{ recursive: true, mode: 0o700 }})
    await writeFile(temporary, encoded, {{ mode: 0o600 }})
    await rename(temporary, catalogPath)
  }} catch {{
    try {{ await unlink(temporary) }} catch {{}}
  }}
}}

function persistCatalog() {{
  catalogWrite = catalogWrite.then(writeCatalog, writeCatalog)
  return catalogWrite
}}

function refreshServers(client) {{
  if (serverRefresh) return serverRefresh
  serverRefresh = (async () => {{
    try {{
      const response = await client.mcp.status()
      const status = response?.data
      if (
        status &&
        typeof status === "object" &&
        !Array.isArray(status) &&
        setServers([...serverNames, ...Object.keys(status)])
      ) {{
        await persistCatalog()
      }}
    }} catch {{}}
  }})().finally(() => {{
    serverRefresh = null
  }})
  return serverRefresh
}}

function publish(sessionID) {{
  return new Promise((resolve) => {{
    let settled = false
    const finish = (ok) => {{
      if (settled) return
      settled = true
      resolve(ok)
    }}
    try {{
      const child = spawn(
        theater,
        ["transcript-receipt", "--strict-exit", "--id", participantID, "--token-file", tokenPath],
        {{ stdio: ["pipe", "ignore", "ignore"] }},
      )
      child.once("error", () => finish(false))
      child.once("close", (code) => finish(code === 0))
      child.stdin.once("error", () => finish(false))
      child.stdin.end(JSON.stringify({{ session_id: sessionID }}))
    }} catch {{
      finish(false)
    }}
  }})
}}

const sleep = (delay) => new Promise((resolve) => setTimeout(resolve, delay))

function denyRules(value) {{
  if (!Array.isArray(value)) return null
  return value.filter(
    (rule) =>
      rule &&
      typeof rule.permission === "string" &&
      typeof rule.pattern === "string" &&
      rule.action === "deny",
  )
}}

async function checkedData(operation, request) {{
  let result
  try {{
    result = await request()
  }} catch (error) {{
    throw new Error(
      "theater approval: " +
        operation +
        " failed: " +
        (error?.message ?? String(error)) +
        " — refusing to run this message unapproved",
    )
  }}
  if (result?.error || !result?.response?.ok || result?.data === undefined) {{
    throw new Error(
      "theater approval: " +
        operation +
        " failed (HTTP " +
        (result?.response?.status ?? "unknown") +
        ": " +
        (result?.error?.name ?? "invalid response") +
        ") — this opencode build may not support safe session permission updates;" +
        " refusing to run unapproved",
    )
  }}
  return result.data
}}

async function protectedPermissionRules(client, sessionID, agentName) {{
  // A safe approval policy may tighten `allow` to `ask`, but must never
  // weaken an explicit native, user, agent, or existing session `deny`.
  // Read the exact effective agent and current session before updating;
  // incompatible APIs fail closed instead of guessing.
  const [session, agents] = await Promise.all([
    checkedData("reading session permissions", () =>
      client.session.get({{ path: {{ id: sessionID }} }}),
    ),
    checkedData("reading agent permissions", () => client.app.agents()),
  ])
  if (!session || session.id !== sessionID) {{
    throw new Error(
      "theater approval: opencode returned the wrong session while checking permissions" +
        " — refusing to run unapproved",
    )
  }}
  const sessionDenies = denyRules(session?.permission ?? [])
  if (sessionDenies === null || !Array.isArray(agents)) {{
    throw new Error(
      "theater approval: opencode returned an incompatible permission shape" +
        " — refusing to run unapproved",
    )
  }}
  const agent = agents.find((candidate) => candidate?.name === agentName)
  const agentDenies = denyRules(agent?.permission)
  if (!agent || agentDenies === null) {{
    throw new Error(
      "theater approval: cannot verify effective permissions for agent " +
        agentName +
        " — refusing to run unapproved",
    )
  }}
  return [...permissionRules, ...sessionDenies, ...agentDenies]
}}

async function enforcePermissions(client, sessionID, agentName) {{
  // Append the launch's approval ruleset to the session's permission and
  // verify it actually landed. The SDK client returns error envelopes
  // instead of throwing (gen/client/client.gen.ts), so an unwatched result
  // can be an HTTP 400 masquerading as success; and a server that ignores
  // the payload returns a plain 200. Either way the session would run
  // unapproved, so every failure throws — native awaits this hook, and the
  // rejection blocks the model call. A failed session is not marked
  // enforced, so the next message retries.
  const protectedRules = await protectedPermissionRules(client, sessionID, agentName)
  const result = await checkedData("updating session permissions", () =>
    client.session.update({{
      path: {{ id: sessionID }},
      body: {{ permission: protectedRules }},
    }}),
  )
  const applied = result?.permission
  if (result?.id !== sessionID) {{
    throw new Error(
      "theater approval: opencode returned the wrong session after updating permissions" +
        " — refusing to run unapproved",
    )
  }}
  const tail = Array.isArray(applied) ? applied.slice(applied.length - protectedRules.length) : []
  const verified =
    tail.length === protectedRules.length &&
    tail.every((rule, index) => {{
      const expected = protectedRules[index]
      return (
        rule &&
        rule.permission === expected.permission &&
        rule.pattern === expected.pattern &&
        rule.action === expected.action
      )
    }})
  if (!verified) {{
    throw new Error(
      "theater approval: opencode accepted the permission update for session " +
        sessionID +
        " but its stored session permission does not end with the rules — " +
        "this opencode build ignores session permission updates; refusing to run unapproved",
    )
  }}
  enforcedSessions.add(sessionID)
}}

async function deliver(sessionID, version) {{
  for (const delay of retryDelays) {{
    if (version !== generation) return
    if (delay > 0) await sleep(delay)
    if (version !== generation) return
    if (await publish(sessionID)) {{
      if (version === generation) deliveredSessionID = sessionID
      return
    }}
  }}
}}

function schedule() {{
  if (!currentSessionID || deliveredSessionID === currentSessionID || publishing) return
  const sessionID = currentSessionID
  const version = generation
  publishing = true
  void deliver(sessionID, version).finally(() => {{
    publishing = false
    if (version !== generation) schedule()
  }})
}}

export const TheaterSessionReceipt = async ({{ client }}) => {{
  return {{
    dispose: async () => {{
      if (serverRefresh) await serverRefresh
      await catalogWrite
    }},
    config: async (config) => {{
      const mcp = config?.mcp
      setServers(mcp && typeof mcp === "object" && !Array.isArray(mcp) ? Object.keys(mcp) : [])
      await persistCatalog()
      void refreshServers(client)
    }},
    "chat.message": async ({{ sessionID }}, output) => {{
      // Approval enforcement, at the final native layer, fail closed. The
      // ruleset is baked in at launch; the hook fires before the session's
      // first LLM call and native awaits the trigger, so a rejection here
      // blocks the model call rather than running unapproved. One append
      // per session — the server merges payload rules after the session's
      // current ones, and repeats would only grow the array.
      if (!permissionRules.length || !sessionID || enforcedSessions.has(sessionID)) return
      const agentName = output?.message?.agent
      if (!agentName) {{
        throw new Error(
          "theater approval: cannot identify the effective agent" +
            " — refusing to run this message unapproved",
        )
      }}
      let flight = enforcementFlights.get(sessionID)
      if (!flight) {{
        flight = enforcePermissions(client, sessionID, agentName)
        enforcementFlights.set(sessionID, flight)
        const clear = () => enforcementFlights.delete(sessionID)
        flight.then(clear, clear)
      }}
      await flight
    }},
    "tool.definition": async ({{ toolID }}) => {{
      if (!boundedName(toolID) || nativeTools.has(toolID)) return
      if (nativeTools.size >= catalogMaxDefinitions) {{
        definitionsComplete = false
        return
      }}
      nativeTools.add(toolID)
    }},
    "tool.execute.before": async ({{ tool }}) => {{
      if (
        mcpTools.has(tool) ||
        nativeTools.has(tool) ||
        unclassifiedTools.has(tool) ||
        !definitionsComplete ||
        !boundedName(tool)
      ) return
      let identity = identifyMcpTool(tool)
      if (!identity) {{
        await refreshServers(client)
        identity = identifyMcpTool(tool)
      }}
      if (!identity) {{
        if (unclassifiedTools.size < catalogMaxDefinitions) unclassifiedTools.add(tool)
        return
      }}
      if (mcpTools.size >= catalogMaxTools) return
      mcpTools.set(tool, identity)
      await persistCatalog()
    }},
    event: async ({{ event }}) => {{
      try {{
        if (event.type === "mcp.tools.changed") void refreshServers(client)
        const info = event?.properties?.info
        if (event.type === "session.created" && info && !info.parentID) {{
          if (typeof info.id !== "string" || !info.id) return
          if (info.id !== currentSessionID) {{
            currentSessionID = info.id
            deliveredSessionID = null
            generation += 1
          }}
        }}
        schedule()
      }} catch {{}}
    }},
  }}
}}
"""


__all__ = ["render_native_plugin"]
