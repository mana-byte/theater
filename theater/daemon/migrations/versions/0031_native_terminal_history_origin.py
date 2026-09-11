"""Persist native history origin and terminal time without assuming old evidence is live."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("native_terminal_evidence", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("from_history", sa.Integer(), server_default=sa.text("1"), nullable=False)
        )
        batch_op.add_column(sa.Column("completed_at", sa.REAL(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("native_terminal_evidence", schema=None) as batch_op:
        batch_op.drop_column("completed_at")
        batch_op.drop_column("from_history")
