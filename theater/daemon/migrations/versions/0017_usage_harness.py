"""Persist harness attribution on usage rows.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "usage",
        sa.Column("harness", sa.Text(), nullable=False, server_default=sa.text("'unknown'")),
    )
    op.execute(
        """
        UPDATE usage
        SET harness = (
            SELECT participants.harness
            FROM participants
            WHERE participants.id = usage.participant_id
        )
        WHERE EXISTS (
            SELECT 1 FROM participants
            WHERE participants.id = usage.participant_id
        )
        """
    )
    op.create_index("idx_usage_harness_ts", "usage", ["harness", "ts"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_usage_harness_ts", table_name="usage")
    op.drop_column("usage", "harness")
