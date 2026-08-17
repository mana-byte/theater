"""Add restore state columns to checkpoints for recovery_restore.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "checkpoints",
        sa.Column("restore_state", sa.Text(), nullable=False, server_default="ready"),
    )
    op.add_column("checkpoints", sa.Column("restore_started_at", sa.REAL(), nullable=True))
    op.add_column("checkpoints", sa.Column("restore_token", sa.Text(), nullable=True))
    op.add_column("checkpoints", sa.Column("restored_at", sa.REAL(), nullable=True))
    op.add_column("checkpoints", sa.Column("restored_by", sa.Text(), nullable=True))
    op.add_column("checkpoints", sa.Column("restore_error", sa.Text(), nullable=True))
    op.add_column("checkpoints", sa.Column("restore_result", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("checkpoints", "restore_result")
    op.drop_column("checkpoints", "restore_error")
    op.drop_column("checkpoints", "restored_by")
    op.drop_column("checkpoints", "restored_at")
    op.drop_column("checkpoints", "restore_token")
    op.drop_column("checkpoints", "restore_started_at")
    op.drop_column("checkpoints", "restore_state")
