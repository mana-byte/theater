"""Add launch_provenance column to participants.

Dropped in 0015 — this migration remains for chain continuity.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("participants", schema=None) as batch_op:
        batch_op.add_column(sa.Column("launch_provenance", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("participants", schema=None) as batch_op:
        batch_op.drop_column("launch_provenance")
