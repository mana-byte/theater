"""Add the checkpoints table and its index.

Plan checkpoints: a snapshot of the jobs table at a point in time,
associated with a participant and a name. AUTOINCREMENT (not bare rowid)
so checkpoint ids are never reused. An index on (participant_id, name)
serves the common lookup pattern.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "checkpoints",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("participant_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("jobs_snapshot", sa.Text(), nullable=False),
        sa.Column("created_at", sa.REAL(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_checkpoints_participant_name",
        "checkpoints",
        ["participant_id", "name"],
    )


def downgrade() -> None:
    op.drop_index("idx_checkpoints_participant_name", table_name="checkpoints")
    op.drop_table("checkpoints")
