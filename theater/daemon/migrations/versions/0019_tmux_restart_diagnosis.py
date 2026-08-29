"""Persist tmux server ownership and restart diagnosis for participants.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("participants", sa.Column("tmux_server_identity", sa.Text(), nullable=True))
    op.add_column("participants", sa.Column("termination_reason", sa.Text(), nullable=True))
    op.add_column("participants", sa.Column("termination_incident", sa.Text(), nullable=True))
    op.add_column("participants", sa.Column("terminated_at", sa.REAL(), nullable=True))


def downgrade() -> None:
    op.drop_column("participants", "terminated_at")
    op.drop_column("participants", "termination_incident")
    op.drop_column("participants", "termination_reason")
    op.drop_column("participants", "tmux_server_identity")
