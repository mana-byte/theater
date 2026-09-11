"""Table definitions, in SQLAlchemy Core.

Core rather than the declarative ORM, on purpose. `theater/models.py` holds
plain dataclasses that every layer passes around freely — the régie renders
them, the MCP server serialises them, `formatting.py` formats them without
importing a UI toolkit. Mapping those declaratively would hang `Mapped[...]`
columns and an identity map off the domain layer to buy nothing: `Store`
already hand-maps rows in `from_row`. Alembic's autogenerate works off this
MetaData exactly as it works off a declarative Base.

Anything changed here needs a matching revision under `migrations/versions/`.
`tests/test_migrations.py` fails the build if the two drift apart.
"""

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
)

Index("idx_participants_pane", participants.c.tmux_pane)
Index("idx_participants_parent", participants.c.parent_id)
Index("idx_participants_status", participants.c.status)
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
    Column("caller_id", Text, nullable=False),
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

# Participant runtime binding: daemon-owned facts needed to recover a native
# runtime without re-deriving identity from the working directory. The row is
# written at launch-intent time (before the backend starts) and updated through
# the exact identity phases. It never stores credentials.
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

# Control operations: one row per durably reserved control. Reserved before
# transmission; DISPATCHED is persisted before the write reaches the wire, so
# an interrupted transmission stays potentially delivered. Job state remains
# running/done/crashed/killed and is separate metadata.
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
    # A native prompt whose execution is still uncertain.  This remains set
    # after its job reaches delivery_unknown, so a later automated prompt
    # cannot cross an execution whose exact native outcome is still unknown.
    Column("execution_barrier", Integer, nullable=False, server_default=text("0")),
    Column("backend_generation", Integer),
    Column("native_session_id", Text),
    Column("native_turn_id", Text),
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

# Native terminal evidence: exactly keyed normalized proof that one native
# turn terminated, sufficient to finish a Theater job after a crash between
# recording evidence and completing the job. First write wins; late evidence
# must not rewrite terminal job state.
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
)

Index("idx_native_terminal_evidence_participant", native_terminal_evidence.c.participant_id)
