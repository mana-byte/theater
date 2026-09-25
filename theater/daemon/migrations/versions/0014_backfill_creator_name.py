"""Idempotently add ``creator_name``, added to 0011 after release (both shapes exist at head).
Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def _has_creator_name() -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # Guard against a DB where checkpoints does not exist yet (unreachable).
    if "checkpoints" not in inspector.get_table_names():
        return False
    return any(c["name"] == "creator_name" for c in inspector.get_columns("checkpoints"))


def upgrade() -> None:
    if _has_creator_name():
        return
    with op.batch_alter_table("checkpoints", schema=None) as batch_op:
        batch_op.add_column(sa.Column("creator_name", sa.Text(), nullable=True))


def downgrade() -> None:
    # No-op: creator_name is owned by revision 0011 which drops it in its own downgrade.
    pass
