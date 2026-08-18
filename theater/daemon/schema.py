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
# The reaper calls list_participants() on every tick and filters out dead
# rows. SQLite will not use a plain index for a != predicate, so the scan
# grows with total history. A partial index makes that cost proportional
# to LIVE participants regardless of how much dead history accumulates:
# measured at 73,000 dead rows, 2.714 ms → 0.097 ms.
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
    # JSON transport persistence. response_format stores the raw serialized
    # JSON schema hint; structured_result stores the complete bare JSON
    # response without clipping; structured_status is null when JSON was not
    # requested and later becomes "parsed" or "unavailable".
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
    # AUTOINCREMENT, not bare rowid: `bus_tail(after_id=...)` uses the id
    # as a read cursor, and plain rowids are reused once the highest row
    # is deleted. A reused id would make a reader skip events it has
    # never seen.
    sqlite_autoincrement=True,
)

Index("idx_bus_ts", bus.c.ts)

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
    # Null sha = file absent at that point: null before is a creation,
    # null after is a deletion. Same before and after means touched but
    # not changed — the pair is what makes drift detection work.
    Column("sha_before", Text),
    Column("sha_after", Text),
    sqlite_autoincrement=True,
)

# Two read patterns: "all rows for this path, newest first" and "all rows
# for this job handle" — the path index serves the first, the job-handle
# index serves the second.
Index("idx_touch_path", touch.c.path)
Index("idx_touch_job", touch.c.job_handle)

# Generic key/value store for daemon state that must outlive derived data —
# most notably the send-sequence counter, which cannot be derived from the
# jobs table once a future GC starts deleting old job rows.
meta = Table(
    "meta",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
)

# Tree-scoped key-value scratchpad. Composite primary key on
# (tree_root_id, repo_root, namespace, key) so each spawn tree can keep
# per-namespace per-key values isolated from every other tree.
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

# Plan checkpoints: a snapshot of the jobs table at a point in time,
# associated with a participant and a name.
checkpoints = Table(
    "checkpoints",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("participant_id", Text, nullable=False),
    Column("creator_name", Text),
    Column("name", Text, nullable=False),
    Column("notes", Text),
    Column("jobs_snapshot", Text, nullable=False),
    Column("created_at", REAL, nullable=False),
    Column("restore_state", Text, nullable=False, server_default=text("'ready'")),
    Column("restore_started_at", REAL),
    Column("restore_token", Text),
    Column("restored_at", REAL),
    Column("restored_by", Text),
    Column("restore_error", Text),
    Column("restore_result", Text),
    Column("restore_claimed_by", Text),
    sqlite_autoincrement=True,
)

Index(
    "idx_checkpoints_participant_name",
    checkpoints.c.participant_id,
    checkpoints.c.name,
)

# Global list is now the default path: ORDER BY created_at DESC LIMIT <=100.
Index("idx_checkpoints_created_at", checkpoints.c.created_at)

# Named shared worktrees: multiple live children can share one linked
# worktree (same directory, same branch, same index/HEAD). The key is
# (repo_root, name) so the same name in two repositories does not collide.
# Only Theater-created named worktrees appear here — a join reuses a row
# the daemon recognises, never an arbitrary pre-existing branch or directory.
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
