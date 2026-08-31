"""Persist source-acknowledged accounting cursors.

Revision ID: 0024
Revises: 0023
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("participants", sa.Column("usage_checkpoint", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("participants", "usage_checkpoint")
