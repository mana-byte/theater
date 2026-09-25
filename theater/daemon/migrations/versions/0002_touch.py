"""Add the touch table for recall: one row per (job, path), nullable shas for create/delete.
Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "touch",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_handle", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("sha_before", sa.Text(), nullable=True),
        sa.Column("sha_after", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index("idx_touch_path", "touch", ["path"])
    op.create_index("idx_touch_job", "touch", ["job_handle"])


def downgrade() -> None:
    op.drop_index("idx_touch_job", table_name="touch")
    op.drop_index("idx_touch_path", table_name="touch")
    op.drop_table("touch")
