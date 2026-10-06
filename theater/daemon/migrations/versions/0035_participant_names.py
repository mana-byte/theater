"""Persist the names of live participants across daemon restarts."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "participant_names",
        sa.Column("participant_id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("participant_names")
