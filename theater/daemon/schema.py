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
    Column("parent_id", Text),
    Column("pid", Integer),
    Column("status", Text, nullable=False),
    Column("last_activity", REAL, nullable=False),
    Column("created_at", REAL, nullable=False),
)

Index("idx_participants_pane", participants.c.tmux_pane)
Index("idx_participants_parent", participants.c.parent_id)
Index("idx_participants_status", participants.c.status)
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
    # Null sha = file absent: null before is creation, null after is deletion.
    Column("sha_before", Text),
    Column("sha_after", Text),
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
