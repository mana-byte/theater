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

#: Session permission rulesets enforcing each approval choice at the last native layer.
#: Native uses the LAST matching rule and session permission merges after config and agent
#: rules, so the plugin appends these plus existing denies (OPENCODE_PERMISSION cannot).
#: `manual` asks for all but native's read allowlist; `edits` adds a trailing `edit: allow`.
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
