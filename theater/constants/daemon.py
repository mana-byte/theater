"""Immutable daemon RPC timings: limits, not user-configurable defaults.
Kept apart from ``theater.config`` so a default and the floor it is measured against never share a
definition.
"""

from __future__ import annotations

#: Ceiling on a single `jobs.await`; five minutes is longer than any turn observed.
RPC_MAX_AWAIT_SECONDS = 300.0

#: How long an await must block before announcement; read at call time so tests can patch it.
RPC_AWAIT_ANNOUNCE_DELAY_SECONDS = 0.25

#: How long a running send job keeps its exclusive claim on a pane; past this it no longer blocks.
SEND_CLAIM_TTL_SECONDS = 300.0

#: Job failure code when a replacement send supersedes an expired send claim.
SEND_SUPERSEDED_ERROR_CODE = "send_superseded"

#: Meta key for the durable send-sequence counter; persisted, never derived from MAX(jobs).
SEND_SEQ_META_KEY = "send_seq"

#: Meta key for the last tmux server identity confirmed by a non-empty inventory.
TMUX_SERVER_IDENTITY_META_KEY = "tmux_server_identity"

#: Meta key prefix for the tmux server identity confirmed by each tmux provider.
TMUX_PROVIDER_IDENTITY_META_PREFIX = "tmux_provider_identity:"

#: One diagnostic row emitted for each detected tmux server replacement.
BUS_KIND_TMUX_SERVER_RESTART = "daemon.tmux_server_restart"

#: Bound the participant ids carried by one tmux server restart diagnostic.
TMUX_SERVER_RESTART_AFFECTED_IDS_LIMIT = 100

#: Participant termination reason for a confirmed tmux server replacement.
TMUX_RESTART_TERMINATION_REASON = "tmux_restart"

#: Job failure code for work interrupted by a confirmed tmux server replacement.
TMUX_RESTART_JOB_ERROR_CODE = "tmux_restarted"

#: Meta key prefix for per-participant receipt tokens; the participant id is appended.
RECEIPT_TOKEN_PREFIX = "receipt_token:"

#: Meta key prefix for participant-scoped native channel credentials.
CHANNEL_CREDENTIAL_PREFIX = "channel_credential:"

#: Audit event for an enabled MCP sidecar omitted for one participant.  Omission
#: is intentionally non-fatal: the harness still starts without that sidecar.
BUS_KIND_MCP_PLUGIN_OMITTED = "mcp_plugin.omitted"

#: Meta key for the stable loopback endpoint used by native OTel channels.
CHANNEL_OTEL_RECEIVER_PORT_META_KEY = "channel_otel_receiver_port"

#: Default time budget for jobs.await when the caller does not specify one.
RPC_DEFAULT_MAX_WAIT_SECONDS = 150.0

#: Read size for incremental git-blob hashing. Hashing never materialises a whole file.
TOUCH_HASH_CHUNK_BYTES = 128 * 1024

#: Per-file ceiling for touch hashes. Larger files are recorded as unavailable, not deleted.
TOUCH_HASH_MAX_FILE_BYTES = 8 * 1024 * 1024

#: Total bytes synchronously hashed at either edge of one job's touch history.
TOUCH_HASH_MAX_JOB_BYTES = 32 * 1024 * 1024

#: Total bytes hashed by one recall query.
RECALL_HASH_MAX_QUERY_BYTES = 32 * 1024 * 1024

#: Byte ceiling on one JSON-encoded recall_read response. MCP bridges cap one frame (Pi: 1 MiB)
#: and an oversized line kills sibling in-flight calls; half leaves envelope headroom. Oldest
#: events are clipped first, with explicit truncation facts.
RECALL_READ_RESPONSE_MAX_BYTES = 512 * 1024


#: Transcript kinds reported by read_transcript and recall_read; ERROR is not a conversation turn.
TRANSCRIPT_READABLE_KINDS = ("assistant", "user", "tool_call", "tool_result")

# Maximum encoded bytes in one transcript read response.
TRANSCRIPT_READ_RESPONSE_MAX_BYTES = 16 * 1024

# Maximum source records loaded into one transcript read page.
TRANSCRIPT_READ_SOURCE_PAGE_LIMIT = 24

# Maximum consecutive empty source pages scanned by one transcript read.
TRANSCRIPT_READ_EMPTY_PAGE_SCAN_LIMIT = 8

#: Bus kind for refused sends; GC protects it from age-based deletion and caps it separately.
BUS_KIND_SEND_REFUSED = "send.refused"

#: Maximum rows returned by one participant-scoped bus page.
BUS_PARTICIPANT_PAGE_MAX_LIMIT = 200

#: Safe default for unfiltered participant history pages.
PARTICIPANTS_LIST_DEFAULT_DEAD_LIMIT = 100

#: Hard ceiling for one explicitly requested participant-list page.
PARTICIPANTS_LIST_MAX_LIMIT = 200

#: Bus kind for an accepted operator or agent kill request.
BUS_KIND_PARTICIPANT_KILL_REQUESTED = "participant.kill_requested"

#: Bus kind for an accepted parent request to interrupt a working child.
BUS_KIND_PARTICIPANT_INTERRUPT_REQUESTED = "participant.interrupt_requested"

#: Bus kind for a new participant crossing a resume/session boundary.
BUS_KIND_PARTICIPANT_SESSION_BOUNDARY = "participant.session_boundary"

#: Bounded participant metadata update; payload names fields, never description prose.
BUS_KIND_PARTICIPANT_METADATA_CHANGED = "participant.metadata_changed"

#: Bus kind for the start of an announced jobs.await wait.
BUS_KIND_JOB_AWAIT_START = "job.await.start"

#: Bus kind for the end of an announced jobs.await wait.
BUS_KIND_JOB_AWAIT_END = "job.await.end"

#: Bus kind for agent observation errors in the transcript-identity audit stream.
BUS_KIND_AGENT_OBSERVATION_ERROR = "agent.observation_error"

#: Bus kind for agent transcript events in the audit stream.
BUS_KIND_AGENT_TRANSCRIPT = "agent.transcript"

#: Bus kinds whose row timestamps describe when Theater observed transcript events.
BUS_KIND_AGENT_USER = "agent.user"
BUS_KIND_AGENT_ASSISTANT = "agent.assistant"
BUS_KIND_AGENT_TOOL_CALL = "agent.tool_call"
BUS_KIND_AGENT_TOOL_RESULT = "agent.tool_result"
BUS_KIND_AGENT_ERROR = "agent.error"
AGENT_OBSERVATION_KINDS = frozenset(
    {
        BUS_KIND_AGENT_USER,
        BUS_KIND_AGENT_ASSISTANT,
        BUS_KIND_AGENT_TOOL_CALL,
        BUS_KIND_AGENT_TOOL_RESULT,
        BUS_KIND_AGENT_ERROR,
    }
)

#: Bus kind for agent transcript receipts in the audit stream.
BUS_KIND_AGENT_TRANSCRIPT_RECEIPT = "agent.transcript_receipt"

#: Bus kind for accepted generic hook envelopes without native payload content.
BUS_KIND_AGENT_HARNESS_EVENT = "agent.harness_event"

#: Bus kind for operator transcript bind events in the audit stream.
BUS_KIND_OPERATOR_TRANSCRIPT_BIND = "operator.transcript_bind"

#: Bus kind for operator transcript unbind events in the audit stream.
BUS_KIND_OPERATOR_TRANSCRIPT_UNBIND = "operator.transcript_unbind"

#: The full set of bus kinds that participate in transcript-identity quarantine audit.
TRANSCRIPT_AUDIT_KINDS = frozenset(
    {
        BUS_KIND_AGENT_OBSERVATION_ERROR,
        BUS_KIND_AGENT_TRANSCRIPT,
        BUS_KIND_AGENT_TRANSCRIPT_RECEIPT,
        BUS_KIND_OPERATOR_TRANSCRIPT_BIND,
        BUS_KIND_OPERATOR_TRANSCRIPT_UNBIND,
    }
)

#: Maximum persisted JSON bytes for one control-operation payload.
CONTROL_OPERATION_PAYLOAD_MAX_BYTES = 65_536

#: Default bound on pending followups per participant; enforced before queueing.
CONTROL_QUEUE_MAX_PENDING = 32

#: The bounded reconciliation window for a native prompt whose transmission or
#: acknowledgement is uncertain.  The operation remains a durable execution
#: barrier after this deadline until exact native evidence or an authoritative
#: idle snapshot clears it; the deadline closes only the affected job.
CONTROL_AMBIGUOUS_DELIVERY_DEADLINE_SECONDS = 30.0

#: Per-participant control-maintenance cadence.  Maintenance tasks are
#: coalesced one-per-participant, so a blocked runtime never serializes another
#: participant's queue or the daemon event loop.
CONTROL_MAINTENANCE_INTERVAL_SECONDS = 0.25

#: Bounded prune batch for control operations and native terminal evidence.
#: The send-sequence counter lives in ``meta`` and survives pruned rows.
RUNTIME_STORAGE_PRUNE_BATCH = 512

#: Maximum encoded UTF-8 bytes one scratchpad value may carry.
SCRATCHPAD_MAX_VALUE_BYTES = 256 * 1024

#: Maximum characters one scratchpad namespace, provided key, or cursor may carry.
SCRATCHPAD_MAX_NAME_LENGTH = 128

#: Maximum number of exact keys one scratchpad.get request may name.
SCRATCHPAD_MAX_KEYS_PER_GET = 128

#: Maximum entries one scratchpad namespace may hold; new-key inserts refuse above it.
SCRATCHPAD_MAX_ENTRIES_PER_NAMESPACE = 512

#: Maximum aggregate encoded bytes one scratchpad namespace may hold across entries.
SCRATCHPAD_NAMESPACE_QUOTA_BYTES = 1024 * 1024

#: Maximum encoded bytes one scratchpad.get page returns; larger reads page by key
#: order via after_key, so an oversized legacy namespace still reads deterministically.
SCRATCHPAD_READ_BUDGET_BYTES = 4 * 1024 * 1024
