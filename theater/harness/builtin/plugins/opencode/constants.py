"""OpenCode-native names and bounded adapter limits."""

OPENCODE_PROVIDER_ID_KEY = "providerID"
OPENCODE_MODEL_ID_KEY = "modelID"

DB_NAME = "opencode.db"
#: 2.x servers resume every claimed session in their database at startup, so each 2.x
#: participant lineage gets its own file and never shares OpenCode's default database.
V2_DB_NAME = "opencode-v2.db"
V2_MARKER_NAME = ".opencode-v2"
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

#: The same policies in 2.x rule shape, applied by the plugin's `permission.evaluate` hook: 2.x
#: returns configured denies before hooks run and appends saved allows after session rules, so
#: only the hook decides last. `grep`/`glob` are 2.x actions of their own, reads in 1.x.
_READ_RULES_V2: tuple[dict[str, str], ...] = (
    {"action": "*", "resource": "*", "effect": "ask"},
    {"action": "read", "resource": "*", "effect": "allow"},
    {"action": "grep", "resource": "*", "effect": "allow"},
    {"action": "glob", "resource": "*", "effect": "allow"},
    {"action": "read", "resource": "*.env", "effect": "ask"},
    {"action": "read", "resource": "*.env.*", "effect": "ask"},
    {"action": "read", "resource": "*.env.example", "effect": "allow"},
)
_APPROVAL_RULES_V2: dict[str, tuple[dict[str, str], ...]] = {
    "manual": _READ_RULES_V2,
    "edits": (*_READ_RULES_V2, {"action": "edit", "resource": "*", "effect": "allow"}),
}
#: Model access and the question form are not tool side effects; approval leaves them native.
APPROVAL_EXEMPT_ACTIONS_V2 = ("provider.use", "question")
#: The 2.x TUI answers asks itself when `cli.json` says `session.permissions: autoaccept`;
#: this env merges over that file for one process, so manual and edits asks reach the human.
TUI_CONFIG_ENV_V2 = "OPENCODE_CLI_CONFIG_CONTENT"
TUI_PROMPTED_PERMISSIONS_V2 = '{"session": {"permissions": "prompt"}}'

#: The legacy bootstrap waits this long for the private server's endpoint banner.
BOOTSTRAP_READY_TIMEOUT_SECONDS = 20.0
#: A freshly booted 2.x location registers its plugins asynchronously; the
#: fail-closed gate polls `GET /api/plugin` within this bound before refusing.
PLUGIN_ACTIVE_TIMEOUT_SECONDS = 15.0
PLUGIN_ACTIVE_POLL_SECONDS = 0.1
#: The legacy bootstrap's own serve credential file, minted 0600 and never in argv.
BOOTSTRAP_CREDENTIAL_NAME = "server-credential"

#: Approval choices whose 1.x enforcement rides on the generated plugin; yolo
#: enforces nothing and never needs the plugin proven loaded.
ENFORCED_APPROVALS = frozenset(("manual", "edits"))
#: The 1.x plugin writes this receipt from its `config` hook, naming the exact
#: build that rendered it; the server runtime refuses a manual/edits session
#: until it appears — OpenCode 1.x keeps serving with a broken plugin.
PLUGIN_LOAD_RECEIPT_FILENAME = "plugin-load-receipt.json"
#: The backend loads its config (and the plugin) lazily on first session work,
#: so the gate polls within this bound before refusing.
PLUGIN_RECEIPT_TIMEOUT_SECONDS = 15.0
PLUGIN_RECEIPT_POLL_SECONDS = 0.1
