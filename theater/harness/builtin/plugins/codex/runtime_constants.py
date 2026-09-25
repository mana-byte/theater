"""Constants for the Codex native runtime."""

from theater.harness.contracts.runtime import NativeTurnTerminal, RuntimeSettingField
from theater.harness.contracts.source import BATCH_TERMINAL_EVIDENCE_MAX

#: Default deadlines from the approved plan (§3.5): 30 s startup, 10 s control.
CODEX_RUNTIME_STARTUP_TIMEOUT_SECONDS = 30.0
CODEX_RUNTIME_CONTROL_TIMEOUT_SECONDS = 10.0

#: Bounded normalization state.
CODEX_RUNTIME_EVENTS_BUFFER = 256
CODEX_RUNTIME_FACTS_BUFFER = 256
CODEX_RUNTIME_OUTCOMES_BUFFER = BATCH_TERMINAL_EVIDENCE_MAX
CODEX_RUNTIME_EVENTS_PER_BATCH = 64
#: Only completions mark the normalized-item ledger; ``item/started`` never
#: does, or the normal started → deltas → completed sequence would be dropped.
CODEX_RUNTIME_COMPLETED_ITEMS_MAX = 1024
CODEX_RUNTIME_TERMINAL_TURNS_MAX = 1024
_CODEX_SETTING_FIELDS = frozenset(RuntimeSettingField)
CODEX_RUNTIME_DELTA_ITEMS_MAX = 32
CODEX_RUNTIME_DELTA_PREVIEW_MAX_CHARS = 2000
#: The synchronous ``thread/resume`` view remains a tiny current-state aid, not history recovery.
CODEX_RUNTIME_RECONCILE_TURNS = 2
#: One paginated history request processes at most this many turn summaries.
CODEX_RUNTIME_RECONCILE_PAGE_SIZE = 16
#: Bound work per pass, not the lifetime of an accepted turn's recovery.
#: The owned task retains its cursor across passes and backs off on failures.
CODEX_RUNTIME_RECONCILE_MAX_PAGES = 64
CODEX_RUNTIME_RECONCILE_PAUSE_SECONDS = 0.05
CODEX_RUNTIME_RECONCILE_RETRY_SECONDS = 0.5
CODEX_RUNTIME_RECONCILE_MAX_RETRY_SECONDS = 5.0
CODEX_RUNTIME_DIAGNOSTICS_MAX = 8
#: Native item revisions are small monotonic counters; anything beyond this
#: bound is treated as the anonymous default rather than trusted as identity.
CODEX_RUNTIME_REVISION_MAX = 1_000_000_000

_LIVE_CHANNEL_ID = "native-live"

#: Deduplicate pending outcomes, committing only after enqueue; cancellation permits loss-free
#: replay.
_PENDING_OUTCOME = object()

_APPROVAL_METHOD_SUFFIX = "requestApproval"
_REQUEST_USER_INPUT_METHOD = "item/tool/requestUserInput"
_CLARIFICATION_METHOD_MARKERS = ("requestUserInput", "elicitation")

_TERMINAL_BY_STATUS = {
    "completed": NativeTurnTerminal.COMPLETED,
    "interrupted": NativeTurnTerminal.INTERRUPTED,
    "failed": NativeTurnTerminal.FAILED,
}

_initialize_params: dict[str, object] = {
    "clientInfo": {"name": "theater", "title": "Theater", "version": "1.0"},
    # thread/settings/update is experimental and capability-gated; request the
    # capability up front so the gate is honest per backend, never presumed.
    "capabilities": {"experimentalApi": True},
}
