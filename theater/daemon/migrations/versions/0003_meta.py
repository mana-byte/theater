"""Add meta (send counter survives job GC; never re-seed from MAX) and a live-participant index.
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
