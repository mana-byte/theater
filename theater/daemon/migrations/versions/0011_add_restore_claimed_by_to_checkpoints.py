"""Add restore_claimed_by to checkpoints; index checkpoints.created_at.

``restore_claimed_by`` records who holds the atomic restore claim while the
restore is in progress. The existing ``idx_checkpoints_participant_name`` no
longer serves the default list path, which is now a global
``ORDER BY created_at DESC LIMIT <=100``; ``idx_checkpoints_created_at``
fills that role.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("checkpoints", schema=None) as batch_op:
        batch_op.add_column(sa.Column("restore_claimed_by", sa.Text(), nullable=True))
        batch_op.create_index("idx_checkpoints_created_at", ["created_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("checkpoints", schema=None) as batch_op:
        batch_op.drop_index("idx_checkpoints_created_at")
        batch_op.drop_column("restore_claimed_by")
