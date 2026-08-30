"""Immutable daemon RPC timings.

Fixed ceilings and delays that are not user-configurable defaults: a default
is a value the user may override in config.toml, a limit is the wall the
override must stay inside. Kept apart from `theater.config` so a setting's
default and the floor it is measured against are not defined in the same
breath.
"""

from __future__ import annotations

#: Ceiling on a single `jobs.await`; five minutes is longer than any turn observed.
RPC_MAX_AWAIT_SECONDS = 300.0

#: How long an await must block before announcement; read at call time so tests can patch it.
RPC_AWAIT_ANNOUNCE_DELAY_SECONDS = 0.25

#: How long a running send job keeps its exclusive claim on a pane; past this it no longer blocks.
SEND_CLAIM_TTL_SECONDS = 300.0

#: Meta key for the durable send-sequence counter; persisted, never derived from MAX(jobs).
SEND_SEQ_META_KEY = "send_seq"

#: Meta key for the last tmux server identity confirmed by a non-empty inventory.
TMUX_SERVER_IDENTITY_META_KEY = "tmux_server_identity"

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

#: Meta key for the stable loopback endpoint used by native OTel channels.
CHANNEL_OTEL_RECEIVER_PORT_META_KEY = "channel_otel_receiver_port"

#: Default time budget for jobs.await when the caller does not specify one.
RPC_DEFAULT_MAX_WAIT_SECONDS = 150.0

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
