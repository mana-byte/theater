"""Add restore_progress column to checkpoints; allow restore_state = 'partial'.

``restore_progress`` holds a JSON blob written after every successfully
restored node during a tree restore. If the daemon crashes or the RPC is
cancelled mid-restore, a subsequent restore attempt reads this blob to
learn which nodes were already spawned, avoiding duplicate processes.

``restore_state`` already holds freeform text in the row; adding 'partial'
as a new terminal value requires no DDL change — only the application layer
is extended to write and understand it. The column is defined with a
CHECK constraint only in new databases; existing databases accept any text
value already. This revision only adds ``restore_progress``.

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
