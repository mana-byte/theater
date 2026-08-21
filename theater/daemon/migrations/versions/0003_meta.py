"""Add the meta table and a partial index on participants.

The meta table is a generic key/value store for daemon state that must
outlive derived data. Its first resident is the send-sequence counter,
which today is derived from `max(jobs.handle)` — a derivation that breaks
once a future GC deletes old job rows: the counter regresses and re-mints
handles that deleted jobs already used. Persisting it independently of the
jobs table fixes that.

The partial index on participants makes the reaper's `list_participants()`
scan proportional to live rows rather than total history. SQLite will not
use a plain index for a `!= 'dead'` predicate; a partial index sidesteps
that, measured at 73,000 dead rows from 2.714 ms to 0.097 ms.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "meta",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_index(
        "idx_participants_live",
        "participants",
        ["created_at"],
        sqlite_where=sa.text("status != 'dead'"),
    )

    # Seed the send-sequence counter from the jobs table; pure-SQL MAX(handle) is lexically wrong.
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT handle FROM jobs WHERE handle LIKE '%#%'")).fetchall()
    best = 0
    for (handle,) in rows:
        _, _, seq = handle.rpartition("#")
        if seq.isdigit():
            best = max(best, int(seq))
    if best > 0:
        bind.execute(
            sa.text("INSERT INTO meta (key, value) VALUES ('send_seq', :val)"),
            {"val": str(best)},
        )


def downgrade() -> None:
    op.drop_index("idx_participants_live", table_name="participants")
    op.drop_table("meta")
