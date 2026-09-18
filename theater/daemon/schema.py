"""Table definitions, in SQLAlchemy Core."""

from __future__ import annotations

from sqlalchemy import REAL, Column, Index, Integer, MetaData, Table, Text, text

metadata = MetaData()

participants = Table(
    "participants",
    metadata,
    Column("id", Text, primary_key=True),
    Column("harness", Text, nullable=False),
    Column("tier", Text, nullable=False),
    Column("tmux_pane", Text),
    Column("tmux_server_identity", Text),
    Column("termination_reason", Text),
    Column("termination_incident", Text),
    Column("terminated_at", REAL),
    Column("cwd", Text),
    Column("branch", Text),
    Column("session_id", Text),
    Column("session_correlation", Text),
    Column("transcript_domain", Text),
    Column("transcript_location", Text),
    Column("resume_floor", Text),
    Column("source_checkpoint", Text),
    Column("resumed_from_id", Text),
    Column("parent_id", Text),
    Column("pid", Integer),
    Column("status", Text, nullable=False),
    Column("last_activity", REAL, nullable=False),
    Column("created_at", REAL, nullable=False),
    Column("description", Text),
    Column("origin", Text, nullable=False, server_default=text("'external'")),
    Column("control_owner_kind", Text, nullable=False, server_default=text("'local_operator'")),
    Column("control_owner_id", Text),
    Column("control_revision", Integer, nullable=False, server_default=text("0")),
    Column("workspace_id", Text),
)

Index("idx_participants_pane", participants.c.tmux_pane)
Index("idx_participants_parent", participants.c.parent_id)
Index("idx_participants_status", participants.c.status)
Index(
    "idx_participants_control_owner",
    participants.c.control_owner_kind,
    participants.c.control_owner_id,
)
Index("idx_participants_workspace", participants.c.workspace_id)
Index(
    "uq_participants_live_resumed_from",
    participants.c.resumed_from_id,
    unique=True,
    sqlite_where=text("status != 'dead' AND resumed_from_id IS NOT NULL"),
)
# Partial index: makes the reaper's list_participants() scan proportional to live rows.
Index(
    "idx_participants_live",
    participants.c.created_at,
    sqlite_where=text("status != 'dead'"),
)

jobs = Table(
    "jobs",
    metadata,
    Column("handle", Text, primary_key=True),
    Column("caller_id", Text),
    Column("actor_client_id", Text),
    Column("actor_participant_id", Text),
    Column("target_id", Text),
    Column("kind", Text, nullable=False),
    Column("prompt", Text),
    Column("state", Text, nullable=False),
    Column("result", Text),
    Column("error_code", Text),
    Column("created_at", REAL, nullable=False),
    Column("finished_at", REAL),
    # JSON transport: response_format, structured_result, structured_status.
    Column("response_format", Text),
    Column("structured_result", Text),
    Column("structured_status", Text),
)

Index("idx_jobs_caller", jobs.c.caller_id)
Index("idx_jobs_actor", jobs.c.actor_client_id, jobs.c.actor_participant_id)
Index("idx_jobs_state", jobs.c.state)

bus = Table(
    "bus",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", REAL, nullable=False),
    Column("from_id", Text),
    Column("to_id", Text),
    Column("kind", Text, nullable=False),
    Column("payload", Text),
    # AUTOINCREMENT not bare rowid: bus_tail(after_id=...) uses id as a read cursor.
    sqlite_autoincrement=True,
)

Index("idx_bus_ts", bus.c.ts)
Index("idx_bus_from_id_id", bus.c.from_id, bus.c.id)
Index("idx_bus_to_id_id", bus.c.to_id, bus.c.id)

budgets = Table(
    "budgets",
    metadata,
    Column("tree_root_id", Text, primary_key=True),
    Column("tokens", Integer, nullable=False, server_default=text("0")),
    Column("cents", Integer, nullable=False, server_default=text("0")),
    Column("limit_cents", Integer),
)

touch = Table(
    "touch",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("job_handle", Text, nullable=False),
    Column("path", Text, nullable=False),
    Column("mode", Text, nullable=False),
    # Null sha without a matching error = file absent.
    Column("sha_before", Text),
    Column("sha_after", Text),
    Column("sha_before_error", Text),
    Column("sha_after_error", Text),
    sqlite_autoincrement=True,
)

# Two read patterns: "all rows for this path, newest first" and "all rows for this job handle".
Index("idx_touch_path", touch.c.path)
Index("idx_touch_job", touch.c.job_handle)

# Generic key/value store for daemon state that must outlive derived data.
meta = Table(
    "meta",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
)

# Tree-scoped scratchpad; optional caller-supplied key updates or inserts.
tree_kv = Table(
    "tree_kv",
    metadata,
    Column("tree_root_id", Text, primary_key=True),
    Column("repo_root", Text, primary_key=True),
    Column("namespace", Text, primary_key=True),
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", REAL, nullable=False),
    Column("updated_by", Text, nullable=False),
)

Index(
    "idx_tree_kv_root",
    tree_kv.c.tree_root_id,
    tree_kv.c.repo_root,
)

participant_artifacts = Table(
    "participant_artifacts",
    metadata,
    Column("participant_id", Text, primary_key=True),
    Column("path", Text, primary_key=True),
    Column("kind", Text, nullable=False),
)

Index("idx_participant_artifacts_participant", participant_artifacts.c.participant_id)

# The daemon stores a verifier, never a plugin credential.  One row is one
# participant-scoped stdio sidecar and remains authoritative after a restart.
participant_mcp_plugins = Table(
    "participant_mcp_plugins",
    metadata,
    Column("participant_id", Text, primary_key=True),
    Column("plugin_name", Text, primary_key=True),
    Column("api_version", Integer, nullable=False),
    Column("credential_id", Text, nullable=False, unique=True),
    Column("credential_verifier", Text, nullable=False),
    Column("grants", Text, nullable=False),
    Column("credential_path", Text, nullable=False),
)

Index("idx_participant_mcp_plugins_participant", participant_mcp_plugins.c.participant_id)

# Named shared worktrees: key is (repo_root, name); only Theater-created worktrees appear here.
named_worktrees = Table(
    "named_worktrees",
    metadata,
    Column("repo_root", Text, primary_key=True),
    Column("name", Text, primary_key=True),
    Column("branch", Text, nullable=False),
    Column("path", Text, nullable=False),
    Column("base_branch", Text),
    Column("created_at", REAL, nullable=False),
)

Index("idx_named_worktrees_path", named_worktrees.c.path)

usage = Table(
    "usage",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("participant_id", Text, nullable=False),
    Column("tree_root_id", Text),
    Column("usage_key", Text),
    Column("ts", REAL, nullable=False),
    Column("model", Text),
    Column("harness", Text, nullable=False, server_default=text("'unknown'")),
    Column("input_tokens", Integer, nullable=False, server_default=text("0")),
    Column("output_tokens", Integer, nullable=False, server_default=text("0")),
    Column("cache_creation_input_tokens", Integer, nullable=False, server_default=text("0")),
    Column("cache_read_input_tokens", Integer, nullable=False, server_default=text("0")),
    Column("reasoning_output_tokens", Integer, nullable=False, server_default=text("0")),
    Column("cost_microcents", Integer, nullable=False, server_default=text("0")),
    sqlite_autoincrement=True,
)

Index("idx_usage_participant", usage.c.participant_id, usage.c.ts)
Index("idx_usage_tree", usage.c.tree_root_id, usage.c.ts)
Index("idx_usage_identity", usage.c.participant_id, usage.c.usage_key, unique=True)
Index("idx_usage_harness_ts", usage.c.harness, usage.c.ts)

# Participant runtime binding: daemon-owned facts needed to recover a native runtime without
# re-deriving identity from the working directory.
participant_runtime_bindings = Table(
    "participant_runtime_bindings",
    metadata,
    Column("participant_id", Text, primary_key=True),
    Column("harness", Text, nullable=False),
    # Selected wiring: "native" or "legacy". "auto" is resolved before persisting.
    Column("wiring", Text, nullable=False),
    # Monotone per-participant generation; identity facts bind to it.
    Column("backend_generation", Integer, nullable=False),
    # intended | started | bound | attached | active | detached | stopped | failed
    Column("lifecycle_phase", Text, nullable=False),
    # Private local endpoint of the detached backend.
    Column("endpoint", Text),
    # Verified process identity; set only after the daemon verified the backend.
    Column("backend_pid", Integer),
    Column("backend_started_at", REAL),
    # Exact native session identity; never cwd-derived.
    Column("native_session_id", Text),
    # Executable/protocol compatibility facts for recovery decisions.
    Column("protocol", Text),
    Column("protocol_version", Text),
    Column("native_version", Text),
    Column("compatibility_policy", Text),
    # Bounded JSON launch-policy facts (approval, model, effort) for recovery.
    Column("launch_policy", Text),
    Column("created_at", REAL, nullable=False),
    Column("updated_at", REAL, nullable=False),
)

Index("idx_runtime_bindings_session", participant_runtime_bindings.c.native_session_id)

# Control operations: one row per durably reserved control.
control_operations = Table(
    "control_operations",
    metadata,
    Column("operation_id", Text, primary_key=True),
    Column("participant_id", Text, nullable=False),
    Column("job_handle", Text),
    # send | steer | queue_followup | settings_update | interrupt
    Column("kind", Text, nullable=False),
    # legacy_tmux | native_runtime
    Column("transport", Text, nullable=False),
    # reserved | queued | dispatched | settled
    Column("delivery_phase", Text, nullable=False),
    # accepted | rejected | unknown; null while delivery is unresolved.
    Column("delivery_result", Text),
    # A native prompt whose execution is still uncertain.
    Column("execution_barrier", Integer, nullable=False, server_default=text("0")),
    Column("backend_generation", Integer),
    Column("native_session_id", Text),
    Column("native_turn_id", Text),
    Column("provider_id", Text),
    Column("provider_generation", Integer),
    Column("terminal_id", Text),
    Column("terminal_incarnation", Text),
    # Queue position from the persisted send-sequence allocator; never
    # MAX(...), timestamps, or an in-memory counter.
    Column("queue_sequence", Integer),
    # Bounded JSON operation payload.
    Column("payload", Text),
    Column("error_code", Text),
    Column("error", Text),
    Column("created_at", REAL, nullable=False),
    Column("updated_at", REAL, nullable=False),
)

Index(
    "idx_control_operations_participant_phase",
    control_operations.c.participant_id,
    control_operations.c.delivery_phase,
)
Index("idx_control_operations_job", control_operations.c.job_handle)
Index(
    "idx_control_operations_execution_barrier",
    control_operations.c.participant_id,
    control_operations.c.execution_barrier,
)
Index(
    "idx_control_operations_queue",
    control_operations.c.participant_id,
    control_operations.c.queue_sequence,
)

# First-write exact terminal proof survives crashes before job completion; late evidence cannot
# rewrite jobs.
native_terminal_evidence = Table(
    "native_terminal_evidence",
    metadata,
    Column("participant_id", Text, primary_key=True),
    Column("backend_generation", Integer, primary_key=True),
    Column("native_session_id", Text, primary_key=True),
    Column("native_turn_id", Text, primary_key=True),
    # completed | failed | interrupted
    Column("terminal", Text, nullable=False),
    Column("result", Text),
    # complete | partial | unavailable
    Column("result_completeness", Text, nullable=False),
    # native_evidence | live_stream | transcript | unknown
    Column("result_provenance", Text, nullable=False),
    Column("error_code", Text),
    Column("error", Text),
    Column("recorded_at", REAL, nullable=False),
    # Existing evidence has no live/history attribution: conservatively
    # treat it as history. New writers always supply the explicit fact.
    Column("from_history", Integer, nullable=False, server_default=text("1")),
    Column("completed_at", REAL),
)

Index("idx_native_terminal_evidence_participant", native_terminal_evidence.c.participant_id)

# Durable terminal-provider identity; generations fence every connected owner.
providers = Table(
    "providers",
    metadata,
    Column("provider_id", Text, primary_key=True),
    Column("selector", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("credential_verifier", Text, nullable=False),
    Column("configuration_version", Integer, nullable=False),
    Column("capabilities", Text, nullable=False),
    Column("limits", Text, nullable=False),
    Column("generation", Integer, nullable=False, server_default=text("0")),
    Column("last_report_revision", Integer),
    Column("created_at", REAL, nullable=False),
    Column("updated_at", REAL, nullable=False),
)

Index("uq_providers_selector", providers.c.selector, unique=True)

terminal_bindings = Table(
    "terminal_bindings",
    metadata,
    Column("participant_id", Text, primary_key=True),
    Column("provider_id", Text, nullable=False),
    Column("provider_generation", Integer, nullable=False),
    Column("terminal_id", Text, nullable=False),
    Column("terminal_incarnation", Text, nullable=False),
    Column("process_facts", Text),
    Column("occupant_evidence", Text, nullable=False),
    Column("health", Text, nullable=False),
    Column("report_revision", Integer, nullable=False),
    Column("created_at", REAL, nullable=False),
    Column("updated_at", REAL, nullable=False),
)

Index(
    "uq_terminal_bindings_identity",
    terminal_bindings.c.provider_id,
    terminal_bindings.c.terminal_id,
    terminal_bindings.c.terminal_incarnation,
    unique=True,
)
Index("idx_terminal_bindings_provider", terminal_bindings.c.provider_id)

public_operations = Table(
    "public_operations",
    metadata,
    Column("operation_id", Text, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("actor_client_id", Text, nullable=False),
    Column("actor_participant_id", Text),
    Column("target_ids", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("phase", Text, nullable=False),
    Column("control_operation_id", Text),
    Column("job_handle", Text),
    Column("result", Text),
    Column("error_code", Text),
    Column("error", Text),
    Column("dispatch_provider_id", Text),
    Column("dispatch_provider_generation", Integer),
    Column("dispatch_terminal_id", Text),
    Column("dispatch_terminal_incarnation", Text),
    Column("dispatch_terminal_occupant_evidence", Text),
    Column("dispatch_terminal_process_facts", Text),
    Column("dispatch_backend_generation", Integer),
    Column("dispatch_native_session_id", Text),
    Column("dispatch_native_turn_id", Text),
    Column("created_at", REAL, nullable=False),
    Column("updated_at", REAL, nullable=False),
    Column("settled_at", REAL),
)

Index("idx_public_operations_state", public_operations.c.state, public_operations.c.updated_at)
Index("idx_public_operations_job", public_operations.c.job_handle)
Index(
    "uq_public_operations_control",
    public_operations.c.control_operation_id,
    unique=True,
    sqlite_where=text("control_operation_id IS NOT NULL"),
)

idempotency_records = Table(
    "idempotency_records",
    metadata,
    Column("client_id", Text, primary_key=True),
    Column("key", Text, primary_key=True),
    Column("method", Text, nullable=False),
    Column("payload_digest", Text, nullable=False),
    Column("operation_id", Text),
    Column("response", Text),
    Column("created_at", REAL, nullable=False),
    Column("settled_at", REAL),
    Column("retain_until", REAL),
)

Index("idx_idempotency_retention", idempotency_records.c.retain_until)
Index("idx_idempotency_operation", idempotency_records.c.operation_id)

workspaces = Table(
    "workspaces",
    metadata,
    Column("workspace_id", Text, primary_key=True),
    Column("ownership_kind", Text, nullable=False),
    Column("owner_id", Text, nullable=False),
    Column("path", Text, nullable=False),
    Column("canonical_repository_root", Text),
    Column("branch", Text),
    Column("resolved_base_commit", Text),
    Column("name", Text),
    Column("state", Text, nullable=False),
    Column("creation_operation_id", Text),
    Column("deletion_operation_id", Text),
    Column("deletion_token", Text),
    Column("deletion_prior_state", Text),
    Column("cleanup_force", Integer),
    Column("cleanup_delete_branch", Integer),
    Column("cleanup_force_branch", Integer),
    Column("cleanup_result", Text),
    Column("created_at", REAL, nullable=False),
    Column("updated_at", REAL, nullable=False),
)

Index(
    "uq_workspaces_active_path",
    workspaces.c.path,
    unique=True,
    sqlite_where=text("state != 'removed'"),
)
Index("idx_workspaces_owner", workspaces.c.ownership_kind, workspaces.c.owner_id)
Index("idx_workspaces_state", workspaces.c.state)
Index("idx_workspaces_creation", workspaces.c.creation_operation_id)

workspace_usages = Table(
    "workspace_usages",
    metadata,
    Column("usage_id", Text, primary_key=True),
    Column("workspace_id", Text, nullable=False),
    Column("holder_kind", Text, nullable=False),
    Column("holder_id", Text, nullable=False),
    Column("acquired_at", REAL, nullable=False),
    Column("released_at", REAL),
    Column("release_reason", Text),
)

Index(
    "idx_workspace_usages_workspace",
    workspace_usages.c.workspace_id,
    workspace_usages.c.acquired_at,
)
Index(
    "uq_workspace_usages_live_holder",
    workspace_usages.c.workspace_id,
    workspace_usages.c.holder_kind,
    workspace_usages.c.holder_id,
    unique=True,
    sqlite_where=text("released_at IS NULL"),
)

launch_reservations = Table(
    "launch_reservations",
    metadata,
    Column("operation_id", Text, primary_key=True),
    Column("participant_id", Text, nullable=False),
    Column("provider_id", Text, nullable=False),
    Column("workspace_usage_id", Text),
    Column("adapter", Text, nullable=False),
    Column("phase", Text, nullable=False),
    Column("launch_facts", Text, nullable=False),
    Column("artifact_refs", Text, nullable=False),
    Column("dispatch_marker", Text),
    Column("created_at", REAL, nullable=False),
    Column("updated_at", REAL, nullable=False),
)

Index("uq_launch_reservations_participant", launch_reservations.c.participant_id, unique=True)
Index("idx_launch_reservations_provider", launch_reservations.c.provider_id)

orchestration_events = Table(
    "orchestration_events",
    metadata,
    Column("sequence", Integer, primary_key=True),
    Column("transaction_id", Text, nullable=False),
    Column("event_index", Integer, nullable=False),
    Column("ending_sequence", Integer, nullable=False),
    Column("kind", Text, nullable=False),
    Column("entity_id", Text, nullable=False),
    Column("entity_revision", Integer, nullable=False),
    Column("payload", Text, nullable=False),
    Column("recorded_at", REAL, nullable=False),
)

Index(
    "uq_orchestration_events_transaction_index",
    orchestration_events.c.transaction_id,
    orchestration_events.c.event_index,
    unique=True,
)
Index("idx_orchestration_events_recorded", orchestration_events.c.recorded_at)

global_scratchpad = Table(
    "global_scratchpad",
    metadata,
    Column("namespace", Text, primary_key=True),
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", REAL, nullable=False),
    Column("expires_at", REAL, nullable=False),
    Column("actor_client_id", Text),
    Column("actor_participant_id", Text),
)

Index("idx_global_scratchpad_expiry", global_scratchpad.c.expires_at)
