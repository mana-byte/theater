"""Add restore_progress (terminal audit blob, not retry state) to checkpoints.
Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("checkpoints", schema=None) as batch_op:
        batch_op.add_column(sa.Column("restore_progress", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("checkpoints", schema=None) as batch_op:
        batch_op.drop_column("restore_progress")
