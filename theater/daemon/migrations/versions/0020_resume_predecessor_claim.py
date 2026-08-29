"""Persist live resume predecessor claims.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("participants", sa.Column("resumed_from_id", sa.Text(), nullable=True))
    op.create_index(
        "uq_participants_live_resumed_from",
        "participants",
        ["resumed_from_id"],
        unique=True,
        sqlite_where=sa.text("status != 'dead' AND resumed_from_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_participants_live_resumed_from", table_name="participants")
    op.drop_column("participants", "resumed_from_id")
