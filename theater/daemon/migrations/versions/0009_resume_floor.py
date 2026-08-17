"""Persist resume floor for successor completion enforcement.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("participants", sa.Column("resume_floor", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("participants", "resume_floor")
