"""Persist a resume boundary for accounting reconciliation.

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("participants", sa.Column("usage_floor", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("participants", "usage_floor")
