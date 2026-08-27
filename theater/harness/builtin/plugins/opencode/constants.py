"""OpenCode-native names and bounded adapter limits."""

OPENCODE_PROVIDER_ID_KEY = "providerID"
OPENCODE_MODEL_ID_KEY = "modelID"

DB_NAME = "opencode-stable.db"
MODELS_TIMEOUT = 20

WORKING_MARKERS = ("esc interrupt", "again to interrupt")
FOOTER_MARKER = "ctrl+p commands"
APPROVAL_MARKER = "Permission required"
QUESTION_MARKER = "esc dismiss"
_SCREEN_TAIL_LINES = 5

STEP_FINISH = "tool-calls"
DRAIN_LIMIT = 500
HISTORY_MESSAGE_BATCH = 200
LIVE_TRAJECTORY_STATE_LIMIT = 2_000

CORRELATION_PLUGIN_SUFFIX = ".opencode.mjs"
CORRELATION_READY_TIMEOUT = 30.0
RECEIPT_RETRY_DELAYS_MS = (0, 100, 500, 2_000)
RECEIPT_SESSION_ID_MAX_BYTES = 256

_WRITE_TOOLS = frozenset({"write", "edit"})
