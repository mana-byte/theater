"""Add restore_progress column to checkpoints; allow restore_state = 'partial'.

``restore_progress`` holds a JSON audit blob written after every node outcome,
including failures and skips. If the daemon crashes or the RPC is cancelled,
the blob preserves the outcomes and side effects known before interruption.
It is not retry state: partial and failed restores are terminal.

``restore_state`` already holds freeform text in the row; adding 'partial'
as a new terminal value requires no DDL change. This revision only adds
``restore_progress``.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("checkpoints", schema=None) as batch_op:
        batch_op.add_column(sa.Column("restore_progress", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("checkpoints", schema=None) as batch_op:
        batch_op.drop_column("restore_progress")
