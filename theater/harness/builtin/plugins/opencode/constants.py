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

#: Permission rules (native `permission` config shape) that enforce each
#: approval choice. OpenCode's build agent merges `"*": "allow"` defaults
#: with every config layer, so a choice that must ask cannot rely on the
#: config file (project-local config merges after OPENCODE_CONFIG); it goes
#: through OPENCODE_PERMISSION, the one layer merged after every file.
#: `edits` allows the `edit` permission, which covers edit/write/apply_patch
#: tool calls; everything else asks the human at the pane.
_APPROVAL_PERMISSIONS = {
    "manual": {"*": "ask"},
    "edits": {"*": "ask", "edit": "allow"},
}
