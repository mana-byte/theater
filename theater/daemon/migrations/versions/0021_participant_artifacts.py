"""Persist ownership of participant-generated filesystem artifacts.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "participant_artifacts",
        sa.Column("participant_id", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("participant_id", "path"),
    )
    op.create_index(
        "idx_participant_artifacts_participant",
        "participant_artifacts",
        ["participant_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_participant_artifacts_participant", table_name="participant_artifacts")
    op.drop_table("participant_artifacts")
