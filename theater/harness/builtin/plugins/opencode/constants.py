"""OpenCode-native names and bounded adapter limits."""

OPENCODE_PROVIDER_ID_KEY = "providerID"
OPENCODE_MODEL_ID_KEY = "modelID"

DB_NAME = "opencode.db"
MODELS_TIMEOUT = 20

WORKING_MARKERS = ("esc interrupt", "again to interrupt")
FOOTER_MARKER = "ctrl+p commands"
APPROVAL_MARKER = "Permission required"
QUESTION_MARKER = "esc dismiss"
_SCREEN_TAIL_LINES = 5

STEP_FINISH = "tool-calls"
#: Native's prompt loop keeps running for these step finishes (prompt.ts
#: excludes both when deciding a turn ended): `unknown` is a provider
#: step-finish reason that continues the turn, not a terminal one.
CONTINUATION_FINISHES = frozenset((STEP_FINISH, "unknown"))
DRAIN_LIMIT = 500
HISTORY_MESSAGE_BATCH = 200
LIVE_TRAJECTORY_STATE_LIMIT = 2_000

CORRELATION_PLUGIN_SUFFIX = ".opencode.mjs"
CORRELATION_READY_TIMEOUT = 30.0
RECEIPT_RETRY_DELAYS_MS = (0, 100, 500, 2_000)
RECEIPT_SESSION_ID_MAX_BYTES = 256

MCP_CATALOG_FILENAME = "mcp-catalog.json"
MCP_CATALOG_VERSION = 1
MCP_CATALOG_MAX_BYTES = 512 * 1024
MCP_CATALOG_MAX_SERVERS = 256
MCP_CATALOG_MAX_TOOLS = 512
MCP_CATALOG_MAX_NON_MCP_TOOLS = 4096
MCP_CATALOG_NAME_MAX_BYTES = 512

_WRITE_TOOLS = frozenset({"write", "edit"})

#: Session permission rulesets (native `PermissionV1.Ruleset` array shape,
#: schema/v1/permission.ts:19-24) that enforce each approval choice at the
#: final native layer.
#:
#: Native evaluates a tool call with the LAST matching rule and falls back
#: to `ask` (permission/index.ts evaluate → findLast). The layers, merged in
#: order: agent defaults (`"*": "allow"`, agent/agent.ts) → global config
#: permissions — every config file deep-merged, with OPENCODE_PERMISSION
#: landing inside that same layer (config.ts:559-561) → the selected agent's
#: own `cfg.agent.<name>.permission` from any config file, merged after the
#: global layer (agent/agent.ts:293) → the SESSION's permission, merged
#: after everything the agent carries (session/llm.ts:149,
#: session/tools.ts:87, session/prompt.ts:346, session/system.ts:120). A
#: session's permission is appendable at runtime through the session update
#: route, whose payload merges last (httpapi handlers/session.ts:194-198).
#: The rendered native plugin appends one of these rulesets followed by every
#: explicit deny from the effective agent and existing session. Manual/edits
#: therefore survive permissive config without weakening native or user
#: denials — the env var could do neither, which is why it is gone.
#:
#: `manual`: every otherwise-allowed tool execution asks the human at the pane.
#: Existing denies remain denies. Native's
#: hardcoded read allowlist (agent/agent.ts defaults: plain `read` tool calls
#: auto-allowed, `.env`-style secret files still ask) is preserved verbatim —
#: a deliberate native allowlist, clearly distinguishable from the permissive
#: `"*": "allow"` default it ships next to, and the same reads-auto-allowed
#: contract as Claude and Codex manual. Everything else — edit, bash, grep,
#: task, even directories native whitelists — asks: at the session layer a
#: user allow rule is indistinguishable from a permissive default, and
#: manual's contract is that nothing runs unattended.
#:
#: `edits` is manual plus one trailing `edit: allow` rule, so otherwise-allowed
#: edit/write/apply_patch calls run unattended (the native `edit` permission
#: covers all three, permission/index.ts:204-206) while bash and everything
#: else still asks; an existing matching deny still wins.
_APPROVAL_SESSION_RULES: dict[str, tuple[dict[str, str], ...]] = {
    "manual": (
        {"permission": "*", "pattern": "*", "action": "ask"},
        {"permission": "read", "pattern": "*", "action": "allow"},
        {"permission": "read", "pattern": "*.env", "action": "ask"},
        {"permission": "read", "pattern": "*.env.*", "action": "ask"},
        {"permission": "read", "pattern": "*.env.example", "action": "allow"},
    ),
    "edits": (
        {"permission": "*", "pattern": "*", "action": "ask"},
        {"permission": "read", "pattern": "*", "action": "allow"},
        {"permission": "read", "pattern": "*.env", "action": "ask"},
        {"permission": "read", "pattern": "*.env.*", "action": "ask"},
        {"permission": "read", "pattern": "*.env.example", "action": "allow"},
        {"permission": "edit", "pattern": "*", "action": "allow"},
    ),
}
