"""Consolidate source checkpoints.

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("participants", sa.Column("source_checkpoint", sa.Text(), nullable=True))
    op.execute(
        "UPDATE participants SET source_checkpoint = COALESCE(usage_checkpoint, usage_floor)"
    )
    op.drop_column("participants", "usage_checkpoint")
    op.drop_column("participants", "usage_floor")


def downgrade() -> None:
    op.add_column("participants", sa.Column("usage_floor", sa.Text(), nullable=True))
    op.add_column("participants", sa.Column("usage_checkpoint", sa.Text(), nullable=True))
    op.execute("UPDATE participants SET usage_checkpoint = source_checkpoint")
    op.drop_column("participants", "source_checkpoint")
