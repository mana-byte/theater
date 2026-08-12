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
    Column("parent_id", Text),
    Column("pid", Integer),
    Column("status", Text, nullable=False),
    Column("last_activity", REAL, nullable=False),
    Column("created_at", REAL, nullable=False),
)

Index("idx_participants_pane", participants.c.tmux_pane)
Index("idx_participants_parent", participants.c.parent_id)
Index("idx_participants_status", participants.c.status)

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
    # AUTOINCREMENT rather than a bare rowid: `bus_tail(after_id=...)` uses the
    # id as a read cursor, and plain rowids are reused once the highest row is
    # deleted. Nothing prunes the bus today, but the day something does, a
    # reused id would make a reader skip events it has never seen.
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
