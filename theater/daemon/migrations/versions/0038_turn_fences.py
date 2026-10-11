"""Durable turn fences: native turns whose jobs ended unverified answer no newer job.

Revision ID: 0038
Revises: 0037
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "turn_fences",
        sa.Column("participant_id", sa.Text(), primary_key=True),
        sa.Column("fence", sa.Text(), primary_key=True),
        sa.Column("job_handle", sa.Text()),
        sa.Column("created_at", sa.REAL(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("turn_fences")
